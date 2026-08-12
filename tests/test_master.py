"""The master agent: dispatch, reply format, and the wiring it reads.

The master is the one component that is both a client of three clouds and a
server to whoever invokes it, so its reply is on the wire twice -- once as the
answer, once as evidence. These tests are about that reply.
"""

import asyncio
import json
from decimal import Decimal

import pytest

from coordinator import master
from coordinator.models import ConversionQuote, ConversionRequest, MeshRun
from coordinator.participants import CLOUD_ENDPOINTS, Participant, build_participants
from protocol.quotes import build_prompt, parse_quotes


def quote(source: str, target: str, rate: str) -> ConversionQuote:
    from datetime import UTC, datetime

    return ConversionQuote(
        source=source,
        source_currency="USD",
        target_currency=target,
        amount=Decimal(100),
        rate=Decimal(rate),
        converted_amount=Decimal(100) * Decimal(rate),
        observed_at=datetime.now(UTC),
        latency_ms=12.0,
    )


def run_with(*, targets=("EUR",), consensus="0.92", failures=None) -> MeshRun:
    from coordinator.consensus import reach_consensus

    request = ConversionRequest(
        amount=Decimal(100), source_currency="USD", target_currencies=list(targets)
    )
    results = [
        reach_consensus(target, [quote(cloud, target, consensus) for cloud in ("gcp", "aws")])
        for target in targets
    ]
    return MeshRun(
        request=request,
        participants=["gcp", "aws", "azure"],
        auth_modes={"gcp": "gcp-wif-aws", "aws": "aws-sigv4-role", "azure": "entra-client-secret"},
        results=results,
        failures=failures or {},
        elapsed_ms=2400.0,
    )


class FakeSource:
    """A participant that answers, or fails, without a network."""

    def __init__(self, rate: str | None) -> None:
        self._rate = rate

    async def convert(self, request: ConversionRequest) -> list[ConversionQuote]:
        if self._rate is None:
            raise RuntimeError("this cloud is down")
        return [quote("fake", target, self._rate) for target in request.target_currencies]


# --------------------------------------------------------------------------
# The reply is the peers' own wire format
# --------------------------------------------------------------------------


def test_the_reply_parses_as_an_ordinary_agent_reply():
    """The master is a drop-in participant: whatever can read a peer can read
    it. If this breaks, the master stops being reachable by its own clients."""
    request = ConversionRequest(
        amount=Decimal(100), source_currency="USD", target_currencies=["EUR", "JPY"]
    )
    text = master.format_run(run_with(targets=("EUR", "JPY"), consensus="0.92"))

    from datetime import UTC, datetime

    quotes = parse_quotes(
        text, request, source="master", latency_ms=1.0, observed_at=datetime.now(UTC)
    )

    assert [q.target_currency for q in quotes] == ["EUR", "JPY"]
    assert quotes[0].rate == Decimal("0.92")


def test_the_envelope_rides_along_without_disturbing_the_parser():
    """The extra fidelity is free: parse_quotes only reads objects carrying a
    target_currency, and the envelope's nested ones are never top-level."""
    text = master.format_run(run_with())
    envelope = json.loads(text.splitlines()[-1])["mesh_run"]

    assert envelope["participants"] == ["gcp", "aws", "azure"]
    assert envelope["auth_modes"]["azure"] == "entra-client-secret"
    assert envelope["elapsed_ms"] == 2400.0


def test_a_target_with_no_consensus_is_absent_rather_than_invented():
    """A caller then gets a named protocol failure for the missing target,
    which is the truth. Emitting a placeholder number would not be."""
    run = run_with(targets=("EUR",))
    run.results[0].quotes = []
    run.results[0].consensus_amount = None
    run.results[0].consensus_rate = None

    text = master.format_run(run)

    assert [line for line in text.splitlines() if line.startswith('{"source_currency"')] == []
    assert '"mesh_run"' in text

    # And the caller learns which target went missing, by name.
    from datetime import UTC, datetime

    from coordinator.errors import AdapterError

    with pytest.raises(AdapterError, match="EUR"):
        parse_quotes(
            text, run.request, source="master", latency_ms=1.0, observed_at=datetime.now(UTC)
        )


def test_failures_survive_into_the_envelope():
    """Which cloud failed and why is the part a bare median throws away."""
    text = master.format_run(run_with(failures={"azure": "authentication: 401"}))
    envelope = json.loads(text.splitlines()[-1])["mesh_run"]

    assert envelope["failures"] == {"azure": "authentication: 401"}


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


def test_a_conversion_prompt_runs_the_mesh(monkeypatch):
    monkeypatch.setattr(
        master, "build_participants", lambda clouds, **kw: [
            Participant(name=cloud, source=FakeSource("0.92"), cloud=cloud) for cloud in clouds
        ]
    )
    request = ConversionRequest(
        amount=Decimal(100), source_currency="USD", target_currencies=["EUR"]
    )

    reply = asyncio.run(master.respond(build_prompt(request)))

    assert '"target_currency": "EUR"' in reply
    envelope = json.loads(reply.splitlines()[-1])["mesh_run"]
    assert envelope["results"][0]["consensus_rate"] == "0.92"


def test_the_matrix_keyword_does_not_run_a_conversion(monkeypatch):
    """Dispatch is on an explicit keyword rather than on whether the conversion
    template matched, so a malformed request is declined as one instead of
    silently starting a five-minute interop sweep."""
    called = []

    async def fake_matrix(text):
        called.append(text)
        return "a table"

    monkeypatch.setattr(master, "run_interop_matrix", fake_matrix)

    assert asyncio.run(master.respond("matrix")) == "a table"
    assert asyncio.run(master.respond("  MATRIX please  ")) == "a table"
    assert len(called) == 2


def test_an_unparseable_prompt_is_declined_and_names_the_other_skill():
    reply = asyncio.run(master.respond("what is the weather"))

    assert "currency conversion" in reply
    assert master.MATRIX_KEYWORD in reply


def test_an_unhandled_failure_comes_back_in_the_reply(monkeypatch):
    """The master is reached over A2A, so an exception otherwise becomes a
    transport error at the caller with the cause in a log they cannot read.
    This project's recurring trap is an error reported at the wrong layer."""

    def explode(*args, **kwargs):
        raise RuntimeError("pool provider is nonsense")

    monkeypatch.setattr(master, "build_participants", explode)
    request = ConversionRequest(
        amount=Decimal(100), source_currency="USD", target_currencies=["EUR"]
    )

    reply = asyncio.run(master.respond(build_prompt(request)))

    assert "master failed" in reply
    assert "pool provider is nonsense" in reply


# --------------------------------------------------------------------------
# Which clouds, and how they are wired
# --------------------------------------------------------------------------


def test_the_mesh_defaults_to_every_cloud(monkeypatch):
    monkeypatch.delenv("CURRENCY_MESH_CLOUDS", raising=False)

    assert master._clouds() == list(CLOUD_ENDPOINTS)


def test_one_cloud_can_be_isolated(monkeypatch):
    """How the negative controls work: the mesh degrades on purpose, so a
    three-cloud run with one credential removed still reaches quorum and
    answers -- which reads as "no denial" and is not."""
    monkeypatch.setenv("CURRENCY_MESH_CLOUDS", "gcp")

    assert master._clouds() == ["gcp"]


def test_a_typo_in_the_cloud_list_is_refused(monkeypatch):
    """Otherwise it becomes a KeyError inside the fan-out, reported as a
    protocol failure on a leg that does not exist."""
    monkeypatch.setenv("CURRENCY_MESH_CLOUDS", "gcp,gpc")

    with pytest.raises(ValueError, match="gpc"):
        master._clouds()


def test_the_master_and_the_cli_wire_the_same_mesh(monkeypatch):
    """Two copies of this wiring is how a hosted run and a local run quietly
    stop measuring the same thing."""
    monkeypatch.delenv("GCP_A2A_AUTH", raising=False)
    monkeypatch.delenv("AWS_A2A_AUTH", raising=False)
    monkeypatch.delenv("AZURE_A2A_AUTH", raising=False)

    participants = build_participants(["gcp", "aws"], client="a2a-sdk", timeout_s=5.0)

    assert [p.name for p in participants] == ["gcp", "aws"]
    assert [p.auth for p in participants] == ["none", "none"]
    assert participants[0].source.endpoint == "http://127.0.0.1:10001"


def test_the_card_advertises_both_skills():
    """A card that under-advertises is the same class of defect as one that
    advertises an unreachable URL."""
    _app, card = master.build()

    assert {skill.id for skill in card.skills} == {"currency_conversion", "interop_matrix"}
    assert card.name == "currency_master"
