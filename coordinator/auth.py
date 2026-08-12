"""One credential seam for every outbound leg of the mesh.

The master runs on **Bedrock AgentCore Runtime**, so every leg starts from an
AWS role. This module holds what all of them share -- the SigV4 signer, the
expiry caches, the provider-response logging, and the registry that answers
"how does this leg authenticate". The three implementations live in
``coordinator.aws_origin``, because what they do not share is the interesting
part::

    AWS -> AWS    SigV4 with the runtime's own role       in-cloud hop
    AWS -> GCP    signed GetCallerIdentity -> GCP STS -> impersonate
    AWS -> Azure  client secret                           **not keyless**

That last row is the price of the host. A Cloud-Run-rooted master could reach
all three keylessly because Cloud Run mints workload OIDC for an arbitrary
audience; an AWS runtime cannot, and Entra has no non-JWT federation path. See
``docs/DEPLOYMENT_PLAN.md``.

``httpx.Auth`` is the shape because it is the only one that spans both a bearer
header and a signature over the request body, and all three vendor client SDKs
accept an ``httpx.AsyncClient``. That also means the **agent-card fetch is
authenticated by the same credential as the call** -- discovery is privileged,
and a card fetch that 403s while the call would have succeeded is the single
most confusing failure in this space.

No vendor SDK is imported here. The master calls three clouds; making it depend
on three clouds' auth libraries to do so would be the wrong trade -- including
AWS's own, which is why the SigV4 signer below is implemented against the
standard rather than pulled from botocore.

Logging
-------
Every provider response is logged at its boundary, in full, on failure. This
is deliberate and it is worth more than the federation work itself: in the
predecessor series nothing cost more time than auth errors that could not be
read -- an adapter that reported an HTTP status and discarded the STS body,
and an error that travelled back as a *tool result* and got paraphrased by the
model into "an issue with the web identity token". A raised message is not an
observable. Raise and log.

Successful responses are logged without their token material.
"""

import hashlib
import hmac
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlparse

import httpx

from coordinator.errors import AdapterError, FailureKind

log = logging.getLogger("coordinator.auth")

#: Refresh this far before actual expiry, so a token cannot die in flight.
_EXPIRY_SKEW = timedelta(seconds=60)


def _auth_error(boundary: str, detail: str) -> AdapterError:
    return AdapterError(FailureKind.AUTHENTICATION, f"{boundary}: {detail}")


def _log_provider_response(boundary: str, response: httpx.Response) -> None:
    """Log what the identity provider actually said.

    On failure the body is logged whole and unparsed. Truncating it is how you
    lose the one line that names the condition that did not match.
    """
    if response.is_success:
        log.info("%s -> %s %s", boundary, response.status_code, response.reason_phrase)
        return
    log.error(
        "%s -> %s %s\nrequest: %s %s\nbody: %s",
        boundary,
        response.status_code,
        response.reason_phrase,
        response.request.method,
        response.request.url,
        response.text,
    )


@dataclass(frozen=True)
class _CachedToken:
    value: str
    expires_at: datetime

    @property
    def usable(self) -> bool:
        return datetime.now(UTC) + _EXPIRY_SKEW < self.expires_at


@dataclass(frozen=True)
class _AwsCredentials:
    access_key_id: str
    secret_access_key: str
    session_token: str
    expires_at: datetime

    @property
    def usable(self) -> bool:
        return datetime.now(UTC) + _EXPIRY_SKEW < self.expires_at


def _parse_expiry(value: str, boundary: str) -> datetime:
    """Read a provider's expiry timestamp as an aware UTC datetime.

    Two traps, both of which surface far from here if left alone. A value with
    no offset parses happily and then raises ``TypeError: can't compare
    offset-naive and offset-aware datetimes`` on the *next* call, inside
    ``usable`` -- a crash at a line that has nothing to do with the cause. And
    an unparseable value raises ``ValueError``, which is not an ``AdapterError``
    and so travels back as an unmapped exception rather than a named auth
    failure. Both providers document UTC, so a naive value is assumed UTC.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise _auth_error(
            boundary, f"could not read the credential expiry {value!r}: {exc}"
        ) from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# --------------------------------------------------------------------------
# SigV4
# --------------------------------------------------------------------------


def _sign_request(
    request: httpx.Request,
    *,
    credentials: _AwsCredentials,
    region: str,
    service: str,
    now: datetime,
    extra_signed_headers: tuple[str, ...] = (),
) -> None:
    """Apply an AWS SigV4 signature to ``request`` in place.

    Implemented against the standard rather than pulled from botocore: the
    master's whole point is that it reaches three clouds without carrying three
    clouds' SDKs.

    ``extra_signed_headers`` names headers already set on the request that must
    fall inside the signature. Two callers need it. AgentCore's session header
    must be signed, or it is a value a proxy could swap. And GCP's Workload
    Identity Federation rejects a ``GetCallerIdentity`` subject token whose
    ``x-goog-cloud-target-resource`` header was not signed, because an unsigned
    one could be redirected at a different pool by anyone who replayed it.
    """
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    url = request.url

    request.headers["host"] = url.netloc.decode("ascii")
    request.headers["x-amz-date"] = amz_date

    signed_names = ["host", "x-amz-date"]
    # Only temporary credentials carry a session token. Sending the header with
    # an empty value -- which is what the env-credentials path produces for
    # long-lived keys -- is signed faithfully and then rejected by AWS, with an
    # error about the signature rather than about the token.
    if credentials.session_token:
        request.headers["x-amz-security-token"] = credentials.session_token
        signed_names.append("x-amz-security-token")
    if "content-type" in request.headers:
        signed_names.append("content-type")
    signed_names.extend(name.lower() for name in extra_signed_headers)
    signed_names = sorted(set(signed_names))

    canonical_headers = "".join(
        f"{name}:{' '.join(request.headers[name].split())}\n" for name in signed_names
    )
    signed_headers = ";".join(signed_names)
    payload_hash = hashlib.sha256(request.content or b"").hexdigest()

    # SigV4 requires each path segment to be URI-encoded *twice* for every
    # service except S3. url.path is percent-decoded, so encoding it once only
    # reproduces the raw path and the signature silently fails to match --
    # invisible on simple paths, fatal on AgentCore, whose invocations URL
    # embeds a percent-encoded ARN. Start from raw_path, which is already
    # encoded once, and encode it again.
    raw_path = url.raw_path.split(b"?", 1)[0].decode("ascii")
    canonical_request = "\n".join(
        [
            request.method,
            _canonical_path(raw_path),
            _canonical_query(url.query.decode("ascii")),
            canonical_headers,
            signed_headers,
            payload_hash,
        ]
    )

    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )

    signing_key = _signing_key(credentials.secret_access_key, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()

    request.headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={credentials.access_key_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )


def _canonical_path(path: str) -> str:
    """Percent-encode the path, leaving ``/`` alone. An empty path signs as ``/``."""
    if not path:
        return "/"
    return quote(path, safe="/-._~")


def _canonical_query(query: str) -> str:
    """Sort query parameters by name then value, with each half re-encoded."""
    if not query:
        return ""
    pairs = []
    for item in query.split("&"):
        name, _, value = item.partition("=")
        pairs.append((_requote(name), _requote(value)))
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))


def _requote(value: str) -> str:
    from urllib.parse import unquote

    return quote(unquote(value), safe="-._~")


def _signing_key(secret: str, date_stamp: str, region: str, service: str) -> bytes:
    key = f"AWS4{secret}".encode()
    for part in (date_stamp, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return key


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------

#: Auth modes a peer can be configured with, keyed by ``{PEER}_A2A_AUTH``.
#: All three are rooted in the master's AWS role and implemented in
#: ``coordinator.aws_origin``; this module keeps the registry so that one
#: function still answers "how does this leg authenticate".
AUTH_MODES = (
    "none",
    "aws-sigv4-role",
    "gcp-wif-aws",
    "entra-client-secret",
)

#: Modes that require no long-lived secret. ``entra-client-secret`` is the only
#: one that does, and the distinction is reported per leg rather than inferred,
#: because "which legs were keyless" is the claim this project has to back --
#: and under an AWS-rooted master that claim now has an exception in it.
KEYLESS_MODES = frozenset({"none", "aws-sigv4-role", "gcp-wif-aws"})


def credentials_for(peer: str, endpoint: str) -> httpx.Auth | None:
    """Return the credential for one outbound leg, or ``None`` for an open peer.

    Configuration is per-peer and environmental, so the same master image runs
    against the local mesh (every peer ``none``) and against the deployed mesh
    without a code change -- which is what keeps the local matrix a protocol
    instrument rather than an identity test.

        AWS_A2A_AUTH=aws-sigv4-role  [AWS_A2A_REGION=<defaults to AWS_REGION>]
                                     [AWS_A2A_SIGNING_SERVICE=bedrock-agentcore]
        GCP_A2A_AUTH=gcp-wif-aws     GCP_A2A_POOL_PROVIDER=//iam.googleapis.com/...
                                     GCP_A2A_SERVICE_ACCOUNT=...
                                     [GCP_A2A_AUDIENCE=<defaults to service root>]
        AZURE_A2A_AUTH=entra-client-secret
                                     AZURE_A2A_TENANT_ID=...  AZURE_A2A_CLIENT_ID=...
                                     AZURE_A2A_CLIENT_SECRET_ARN=... (or _SECRET)
    """
    prefix = peer.upper()
    mode = os.getenv(f"{prefix}_A2A_AUTH", "none").strip().lower()

    if mode == "none":
        return None
    if mode not in AUTH_MODES:
        raise AdapterError(
            FailureKind.VALIDATION,
            f"unknown auth mode {mode!r} for peer {peer} (expected one of {AUTH_MODES})",
        )

    # Imported here rather than at module scope: aws_origin imports the signer
    # and the cache types from this module, and a top-level import would be
    # circular.
    from coordinator import aws_origin

    return aws_origin.build(peer, mode, endpoint)


#: AgentCore Runtime isolates sessions on this header and requires it on every
#: request -- including the agent-card fetch, which is served from the same
#: ``/invocations/`` path. It must be at least 33 characters.
AGENTCORE_SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


def agentcore_headers(prefix: str) -> dict[str, str]:
    """The session header AgentCore requires, stable for the life of the run.

    One session per master process rather than per request: AgentCore keeps a
    runtime session alive per ID, and minting a fresh one on every call would
    create a session per quote and burn the session quota. It also costs real
    latency -- each new session gets its own microVM, and a cold one accounted
    for the ~5.9s anomaly this project spent an evening on and then retracted.
    """
    import uuid

    session_id = os.getenv(f"{prefix}_A2A_SESSION_ID") or str(uuid.uuid4())
    if len(session_id) < 33:
        raise AdapterError(
            FailureKind.VALIDATION,
            f"{prefix}_A2A_SESSION_ID must be at least 33 characters "
            f"(AgentCore rejects shorter ones); got {len(session_id)}",
        )
    return {AGENTCORE_SESSION_HEADER: session_id}


def auth_mode(auth: httpx.Auth | None) -> str:
    """Name the mode on a credential, for the matrix report and the CLI header."""
    return getattr(auth, "mode", "none") if auth is not None else "none"


def is_keyless(mode: str) -> bool:
    """Whether a leg running in ``mode`` needs a long-lived secret.

    Reported per leg rather than inferred from the topology afterwards. The
    whole claim of this project is which legs were keyless, and under an
    AWS-rooted master one of them is not -- so a run that used a client secret
    must never be summarised as though it had not.
    """
    return mode in KEYLESS_MODES


def _service_root(endpoint: str) -> str:
    """Scheme and host only -- Cloud Run's ID token audience is the service URL."""
    parsed = urlparse(endpoint)
    if not parsed.scheme or not parsed.netloc:
        return endpoint.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}"
