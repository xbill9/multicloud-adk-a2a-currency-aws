"""The single interface every cloud plugs into.

The two-cloud benchmarks had two protocols, ``ExchangeRateTool`` and
``RemoteCurrencyAgent``, with identical shapes -- an artifact of MCP and A2A
being wired in one at a time. A mesh has no such asymmetry: a local MCP tool
and a remote agent on another continent are both just named quote sources.

``QuoteSource`` deliberately says nothing about credentials. The credential is
a property of the *leg*, not of the conversion, and it is resolved once when
the source is constructed -- ``credentials_for(peer, endpoint)`` in
``coordinator.auth``, re-exported here because this is the interface it hangs
off. One adapter, three implementations, one shape; adding a fourth cloud
means adding a mode, not a code path.

The alternative is what the predecessor series did: three bespoke auth paths
retrofitted after the fact, one per repo, which is why its findings ended up
scattered across six of them.

``build_participants`` lives here rather than in the CLI because the CLI is no
longer the only thing that assembles a mesh: the master agent
(``coordinator.master``) assembles the same one from the same environment, and
two copies of that wiring is how a hosted run and a local run quietly stop
measuring the same thing.
"""

import os
from dataclasses import dataclass
from typing import Protocol

from coordinator.auth import auth_mode, credentials_for
from coordinator.models import ConversionQuote, ConversionRequest


class QuoteSource(Protocol):
    async def convert(self, request: ConversionRequest) -> list[ConversionQuote]: ...


@dataclass(frozen=True)
class Participant:
    """A named quote source, plus the metadata the matrix report needs."""

    name: str
    source: QuoteSource
    cloud: str = "local"
    stack: str = "in-process"
    #: How this leg authenticates: one of ``coordinator.auth.AUTH_MODES``.
    #: Reported rather than inferred, so a leg that silently fell back to an
    #: unauthenticated call cannot be mistaken for a federated one.
    auth: str = "none"

    def __str__(self) -> str:
        return self.name


#: Where each cloud's agent is, and the variable that overrides it. The
#: defaults are the local mesh (``./infra/run_mesh.sh start``), so an
#: unconfigured process measures loopback rather than failing to resolve.
CLOUD_ENDPOINTS = {
    "gcp": ("GCP_A2A_ENDPOINT", "http://127.0.0.1:10001"),
    "aws": ("AWS_A2A_ENDPOINT", "http://127.0.0.1:10002"),
    "azure": ("AZURE_A2A_ENDPOINT", "http://127.0.0.1:10003"),
}


def build_participants(
    clouds: list[str] | None = None,
    *,
    client: str = "a2a-sdk",
    timeout_s: float = 60.0,
) -> list["Participant"]:
    """Wire one participant per cloud, resolving each leg's credential once."""
    # Deferred so this module stays importable without any vendor SDK present.
    from clients import load_client

    participants: list[Participant] = []
    for cloud in clouds or list(CLOUD_ENDPOINTS):
        env_var, default = CLOUD_ENDPOINTS[cloud]
        endpoint = os.getenv(env_var, default)
        auth = credentials_for(cloud, endpoint)
        participants.append(
            Participant(
                name=cloud,
                source=load_client(
                    client, endpoint, source=cloud, timeout_s=timeout_s, auth=auth
                ),
                cloud=cloud,
                stack=client,
                auth=auth_mode(auth),
            )
        )
    return participants


__all__ = [
    "CLOUD_ENDPOINTS",
    "Participant",
    "QuoteSource",
    "auth_mode",
    "build_participants",
    "credentials_for",
]
