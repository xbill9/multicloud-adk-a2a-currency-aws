"""The master, served as an A2A agent on Bedrock AgentCore Runtime.

The master used to be a Cloud Run *job*: a process that started, ran one
conversion, printed it and exited. Moving it to AgentCore changes its shape as
well as its cloud, because AgentCore hosts servers, not jobs -- so the thing
that fans out across three clouds is now itself an agent, reached over the same
protocol it uses to reach the other three.

That is a better fit than it sounds. The master answers the *same* prompt
template the peers answer, and replies in the *same* wire format, so it is a
drop-in participant: any of the three client stacks can drive it, and its reply
parses with ``protocol.quotes.parse_quotes`` like any other agent's. The
difference is only what is behind it -- three clouds and a median, instead of a
rate table or a model.

Two skills:

``currency_conversion``
    Run the mesh and reply with one consensus quote per target, plus a trailing
    ``{"mesh_run": ...}`` object carrying the full envelope: which clouds
    answered, what each said, the auth mode per leg, and the elapsed time.
    Peers' parsers ignore that object -- they only read objects carrying a
    ``target_currency`` -- so the extra fidelity costs nothing on the wire.

``interop_matrix``
    Run the 3x3 and reply with the rendered table. This lived in a second Cloud
    Run job before; there is no job to put it in now, and a second runtime for
    one entrypoint would be a deployment to keep in step for no gain.

Configuration
-------------
``CURRENCY_MESH_CLOUDS``   comma-separated subset, default all three
``CURRENCY_MESH_CLIENT``   client stack used for every leg, default a2a-sdk
``CURRENCY_MESH_TIMEOUT``  per-leg timeout in seconds, default 60
plus the per-peer endpoint and auth variables read by ``coordinator.auth``.

``CURRENCY_MESH_CLOUDS`` is how the negative controls isolate a single leg. It
matters that they can: the mesh degrades on purpose, so a three-cloud run with
one credential removed still reaches quorum on the other two and answers
normally -- which reads as "no denial" and is not.
"""

import json
import logging
import os

from a2a.types import AgentSkill

from agents.common import parse_conversion_prompt, public_url
from agents.serving import CallbackExecutor, build_agent_card, build_app
from coordinator.mesh import CurrencyMesh
from coordinator.models import MeshRun
from coordinator.participants import CLOUD_ENDPOINTS, build_participants

log = logging.getLogger("coordinator.master")

DEFAULT_PORT = 10000
AGENT_NAME = "currency_master"

DESCRIPTION = (
    "Answers currency conversion questions by asking a Google Cloud, an AWS and "
    "an Azure agent the same question over A2A and reporting the median"
)

#: Prefix that selects the matrix skill. Dispatch is on an explicit keyword
#: rather than on whether the conversion template matched, so a malformed
#: conversion request is declined as a malformed conversion request instead of
#: silently running a five-minute interop sweep.
MATRIX_KEYWORD = "matrix"


def _clouds() -> list[str]:
    """Which peers this master fans out to, validated rather than assumed.

    A typo here would otherwise become a KeyError inside the fan-out, reported
    as a protocol failure on a leg that does not exist.
    """
    raw = os.getenv("CURRENCY_MESH_CLOUDS", "").strip()
    if not raw:
        return list(CLOUD_ENDPOINTS)
    clouds = [part.strip().lower() for part in raw.split(",") if part.strip()]
    unknown = [cloud for cloud in clouds if cloud not in CLOUD_ENDPOINTS]
    if unknown:
        raise ValueError(
            f"CURRENCY_MESH_CLOUDS names unknown clouds {unknown} "
            f"(expected any of {list(CLOUD_ENDPOINTS)})"
        )
    return clouds


def _client() -> str:
    return os.getenv("CURRENCY_MESH_CLIENT", "a2a-sdk").strip().lower()


def _timeout() -> float:
    return float(os.getenv("CURRENCY_MESH_TIMEOUT", "60"))


def format_run(run: MeshRun) -> str:
    """Render a mesh run as the peers' wire format, plus the whole envelope.

    The consensus rate is what a caller asked for; the envelope is what makes
    the answer checkable. A target that reached no consensus is simply absent
    from the quote lines -- a client parsing this gets a named protocol failure
    for the missing target, which is the truth, rather than a fabricated
    number.
    """
    lines = []
    for result in run.results:
        if result.consensus_amount is None or result.consensus_rate is None:
            continue
        lines.append(
            json.dumps(
                {
                    "source_currency": run.request.source_currency,
                    "target_currency": result.target_currency,
                    "rate": str(result.consensus_rate),
                    "converted_amount": str(result.consensus_amount),
                }
            )
        )
    lines.append(json.dumps({"mesh_run": json.loads(run.model_dump_json())}))
    return "\n".join(lines)


async def run_conversion(text: str) -> str:
    request = parse_conversion_prompt(text)
    if request is None:
        return (
            "I can only help with currency conversion and exchange rates, or with "
            f"the interop matrix -- send a message beginning with {MATRIX_KEYWORD!r} "
            "for that."
        )

    clouds = _clouds()
    participants = build_participants(clouds, client=_client(), timeout_s=_timeout())
    log.info(
        "mesh run: %s %s -> %s across %s via %s",
        request.amount,
        request.source_currency,
        ", ".join(request.target_currencies),
        ", ".join(clouds),
        _client(),
    )
    run = await CurrencyMesh(participants, timeout_seconds=_timeout()).run(request)
    for name, failure in run.failures.items():
        log.error("leg %s failed: %s", name, failure)
    return format_run(run)


async def run_interop_matrix(text: str) -> str:
    """Fill the client x server grid from here, over the same credential seam.

    Deliberately reuses ``matrix.runner`` rather than reimplementing the sweep:
    a matrix that measured a different mesh than the consensus run measures
    would be worse than no matrix.
    """
    from decimal import Decimal

    from clients import CLIENT_STACKS
    from coordinator.models import ConversionRequest
    from matrix.runner import DEFAULT_SERVERS, render_table, run_matrix

    clouds = set(_clouds())
    servers = tuple(server for server in DEFAULT_SERVERS if server.name in clouds)
    request = ConversionRequest(
        amount=Decimal("100"), source_currency="USD", target_currencies=["EUR", "GBP"]
    )
    report = await run_matrix(servers, CLIENT_STACKS, request, timeout_s=_timeout() * 2)
    return render_table(report)


async def respond(text: str) -> str:
    """One A2A message in, one reply out, dispatched on the leading keyword."""
    try:
        if text.strip().lower().startswith(MATRIX_KEYWORD):
            return await run_interop_matrix(text)
        return await run_conversion(text)
    except Exception as exc:  # noqa: BLE001 - a reply is the only channel there is
        # The master is reached over A2A, so an unhandled exception becomes a
        # transport-level failure at the caller with the cause left in a log the
        # caller cannot read. This project's recurring trap is an error reported
        # at the wrong layer; carry it back in the reply instead.
        log.exception("master failed to answer")
        return f"master failed: {type(exc).__name__}: {exc}"


def build() -> tuple:
    card = build_agent_card(
        name=AGENT_NAME,
        url=public_url(DEFAULT_PORT),
        description=DESCRIPTION,
        skills=[
            AgentSkill(
                id="currency_conversion",
                name="currency conversion by three-cloud consensus",
                description=(
                    "Ask Google Cloud, AWS and Azure the same conversion question over "
                    "A2A and reply with the median, plus the full run envelope."
                ),
                tags=["currency", "exchange-rate", "finance", "brain:mesh"],
            ),
            AgentSkill(
                id="interop_matrix",
                name="A2A interop matrix",
                description=(
                    f"Send a message beginning with {MATRIX_KEYWORD!r} to run every "
                    "client stack against every cloud's agent and get the table back."
                ),
                tags=["interop", "a2a", "matrix"],
            ),
        ],
    )
    return build_app(CallbackExecutor(respond), card), card


app, _card = build()


def log_credential_source() -> None:
    """Say what AWS environment the runtime handed us, at start.

    **Names only, never values.** Every AWS_* name is dumped rather than a fixed
    list, because the fixed list is what made the first deployed run ambiguous:
    it reported the absence of the four names it knew about and could not say
    what was there instead. Measured 2026-08-12, the answer on AgentCore Runtime
    is exactly one name, ``AWS_REGION`` -- no container credential endpoint and
    no keys, so the credentials come from IMDS.

    Two of the three legs are signed with this runtime's role, so "where did the
    credentials come from" is the first question asked when a deployed leg
    fails, and a log line at start beats inferring it from a signature mismatch.
    """
    names = sorted(name for name in os.environ if name.startswith("AWS_"))
    log.info("aws environment (names only): %s", ", ".join(names) or "<empty>")
    if not any(
        name
        in (
            "AWS_CONTAINER_CREDENTIALS_FULL_URI",
            "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
            "AWS_ACCESS_KEY_ID",
        )
        for name in names
    ):
        log.info(
            "no container credential endpoint and no keys in the environment; "
            "credentials will come from IMDS, and the role it serves is logged "
            "when they are resolved."
        )


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    log_credential_source()
    uvicorn.run(
        app,
        host=os.getenv("HOST", "127.0.0.1"),
        port=int(os.getenv("PORT", str(DEFAULT_PORT))),
    )


if __name__ == "__main__":
    main()
