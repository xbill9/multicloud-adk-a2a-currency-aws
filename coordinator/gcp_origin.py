"""The outbound legs of a GCP-rooted master.

The master runs as a **Cloud Run service**, so all three legs start from the same
place -- a Google service account, reachable through the metadata server -- and
all three are keyless:

    GCP -> GCP    metadata ID token, audience = the service URL   in-cloud hop
    GCP -> AWS    metadata ID token -> AssumeRoleWithWebIdentity -> SigV4
    GCP -> Azure  metadata ID token -> Entra FIC client assertion

``coordinator.auth`` holds what these share with the AWS-rooted legs: the
SigV4 signer, the caches, and the registry that dispatches to ``build`` below.

Why all three are keyless here and only two are from AWS
--------------------------------------------------------
One capability, and it belongs to the host rather than to this code: **Cloud Run
mints a workload OIDC token for an arbitrary audience.** Ask the metadata server
for ``?audience=api://AzureADTokenExchange`` and it hands back a Google-signed
JWT for exactly that audience. Entra's Federated Identity Credential wants a JWT
assertion from an issuer with OIDC discovery, and ``accounts.google.com`` is
one, so the Azure leg federates and carries no secret.

An AgentCore execution role is not an OIDC issuer, which is why the same leg in
``aws_origin`` falls back to a client secret. That asymmetry is the measured
result the two topologies exist to compare, so nothing here should quietly grow
a secret fallback: if the mint fails, this module fails loudly.

The AWS leg is the mirror image of ``aws_origin.GcpFederatedIdTokenAuth``.
There, Google accepts an AWS-shaped subject token. Here, AWS accepts a Google
OIDC token directly -- ``accounts.google.com`` is a federation partner AWS knows
natively, and creating an explicit IAM OIDC provider for it *breaks* the trust
with ``InvalidIdentityToken``. Opposite rule from Entra, same-looking task.

Stale wiring across origins
---------------------------
Both origin modules ship in one image, and a mode is only valid for the host it
was written for: ``google-id-token`` on AgentCore reaches a metadata server that
is not there. ``build`` therefore records the origin it resolved, and
``log_origin`` prints it at start, so "which origin is this leg using" is an
observable rather than an assumption -- the same reasoning that put the resolved
IAM role name in the AWS-rooted logs.
"""

import base64
import binascii
import json
import logging
import os
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree

import httpx

from coordinator.auth import (
    _auth_error,
    _AwsCredentials,
    _CachedToken,
    _log_provider_response,
    _parse_expiry,
    _service_root,
    _sign_request,
    agentcore_headers,
)
from coordinator.errors import AdapterError, FailureKind

log = logging.getLogger("coordinator.gcp_origin")

#: The metadata server. ``format=full`` is not optional: without it Google
#: trims the token and omits the ``email`` claim, which the AWS trust policy
#: and the Entra FIC both match on via ``sub``/``oaud``.
_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/identity"
)
_METADATA_HEADERS = {"Metadata-Flavor": "Google"}

#: AWS names itself as the audience for a web-identity token. This is the
#: token's ``aud``, which the role's trust policy reads as
#: ``accounts.google.com:oaud`` -- *not* ``:aud``, which holds the numeric
#: ``azp`` and can never match an audience string.
_AWS_STS_AUDIENCE = "sts.amazonaws.com"
_STS_API_VERSION = "2011-06-15"
_STS_XML_NS = {"sts": f"https://sts.amazonaws.com/doc/{_STS_API_VERSION}/"}

#: Entra's fixed audience for a federated client assertion.
_ENTRA_EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"
_ENTRA_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"

_AADSTS_CODE = re.compile(r"AADSTS(\d+)")
_AADSTS_HINTS = {
    "70021": "no federated credential matched -- check the FIC's issuer and subject",
    "700212": "the assertion's audience is not api://AzureADTokenExchange",
    "700213": "the assertion's audience is not api://AzureADTokenExchange",
    "7000215": "Entra read this as a client secret, not an assertion",
}


def _jwt_expiry(token: str, boundary: str) -> datetime:
    """Read ``exp`` out of an unverified JWT.

    We are the token's *holder*, not its verifier, so there is nothing to check
    a signature against and nothing gained by doing so -- the callee verifies.
    All this decides is when to mint the next one, and a wrong answer here costs
    a retry rather than a security property.
    """
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromtimestamp(int(claims["exp"]), UTC)
    except (IndexError, KeyError, ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise _auth_error(
            boundary, f"could not read exp from the minted token: {type(exc).__name__}: {exc}"
        ) from exc


class GoogleWorkloadIdentity:
    """Mints Google-signed OIDC tokens for whatever audience is asked for.

    One instance per leg, caching per audience, because the three legs want
    three different audiences and a token minted for one is refused by the
    others -- audience being the only thing that stops an Azure-bound assertion
    from being replayed at AWS.
    """

    def __init__(
        self,
        *,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._transport = transport
        self._cache: dict[str, _CachedToken] = {}

    async def id_token(self, audience: str) -> str:
        cached = self._cache.get(audience)
        if cached is not None and cached.usable:
            return cached.value

        boundary = f"gcp metadata id token (audience={audience})"
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.get(
                    _METADATA_IDENTITY_URL,
                    params={"audience": audience, "format": "full"},
                    headers=_METADATA_HEADERS,
                )
        except httpx.HTTPError as exc:
            log.error("%s -> unreachable: %s: %s", boundary, type(exc).__name__, exc)
            raise _auth_error(
                boundary,
                f"cannot reach the metadata server at {_METADATA_IDENTITY_URL}: {exc}. "
                "This leg's mode is GCP-rooted; on a non-GCP host it can never succeed.",
            ) from exc

        _log_provider_response(boundary, response)
        if not response.is_success:
            raise _auth_error(boundary, f"metadata mint refused: {response.text}")

        token = response.text.strip()
        if not token:
            raise _auth_error(boundary, "the metadata server returned an empty token")

        self._cache[audience] = _CachedToken(token, _jwt_expiry(token, boundary))
        return token


class GoogleIdTokenAuth(httpx.Auth):
    """GCP -> GCP. The in-cloud hop, and the cheapest leg in either topology.

    Cloud Run validates an ID token whose ``aud`` is the receiving service's own
    URL, so the audience is the service root rather than the full endpoint --
    a token minted for ``https://host/a2a`` is refused by ``https://host``.
    """

    def __init__(
        self,
        *,
        audience: str,
        identity: GoogleWorkloadIdentity | None = None,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._audience = audience
        self._identity = identity or GoogleWorkloadIdentity(
            timeout_s=timeout_s, transport=transport
        )

    @property
    def mode(self) -> str:
        return "google-id-token"

    def sync_auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        raise RuntimeError("the mesh is async; use an httpx.AsyncClient")

    async def async_auth_flow(self, request: httpx.Request):
        token = await self._identity.id_token(self._audience)
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


class AwsWebIdentitySigV4Auth(httpx.Auth):
    """GCP -> AWS. A Google OIDC token traded for role credentials, then SigV4.

    Two hops, and the failure modes are named differently at each. AWS returns
    ``InvalidIdentityToken`` when it could not validate the token at all --
    which is what an explicit ``accounts.google.com`` IAM OIDC provider causes,
    because AWS federates with Google natively and an explicit provider breaks
    it. ``AccessDenied`` means the token was read and the trust conditions did
    not match, which is a policy problem. Confusing those two costs an
    afternoon.
    """

    def __init__(
        self,
        *,
        role_arn: str,
        region: str,
        service: str,
        session_name: str = "currency-mesh-master",
        extra_headers: dict[str, str] | None = None,
        identity: GoogleWorkloadIdentity | None = None,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._role_arn = role_arn
        self._region = region
        self._service = service
        self._session_name = session_name
        self._extra_headers = extra_headers or {}
        self._identity = identity or GoogleWorkloadIdentity(
            timeout_s=timeout_s, transport=transport
        )
        self._timeout_s = timeout_s
        self._transport = transport
        self._cache: _AwsCredentials | None = None

    @property
    def mode(self) -> str:
        return "aws-sigv4"

    def sync_auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        raise RuntimeError("the mesh is async; use an httpx.AsyncClient")

    async def async_auth_flow(self, request: httpx.Request):
        credentials = await self._role_credentials()
        for name, value in self._extra_headers.items():
            request.headers[name] = value
        _sign_request(
            request,
            credentials=credentials,
            region=self._region,
            service=self._service,
            now=datetime.now(UTC),
            extra_signed_headers=tuple(self._extra_headers),
        )
        yield request

    async def _role_credentials(self) -> _AwsCredentials:
        if self._cache is not None and self._cache.usable:
            return self._cache

        token = await self._identity.id_token(_AWS_STS_AUDIENCE)
        boundary = f"aws assume-role-with-web-identity (role={self._role_arn})"
        endpoint = f"https://sts.{self._region}.amazonaws.com/"
        form = {
            "Action": "AssumeRoleWithWebIdentity",
            "Version": _STS_API_VERSION,
            "RoleArn": self._role_arn,
            "RoleSessionName": self._session_name,
            "WebIdentityToken": token,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                # Unsigned by design: the web identity token *is* the credential.
                response = await client.post(endpoint, data=form)
        except httpx.HTTPError as exc:
            log.error("%s -> unreachable: %s: %s", boundary, type(exc).__name__, exc)
            raise _auth_error(boundary, f"cannot reach {endpoint}: {exc}") from exc

        _log_provider_response(boundary, response)
        if not response.is_success:
            raise _auth_error(boundary, _sts_detail(response))

        self._cache = _parse_sts_credentials(response.text, boundary)
        return self._cache


class EntraFederatedAuth(httpx.Auth):
    """GCP -> Azure. Keyless, and the leg that does not survive a move to AWS.

    Entra's Federated Identity Credential takes a JWT assertion from an issuer
    it can discover. Google is one, so the Google-minted token *is* the client
    credential and nothing long-lived exists. ``aws_origin`` cannot do this and
    carries a client secret instead; keep the two modes distinct in anything
    that reports them, because ``entra-fic`` and ``entra-client-secret`` are
    exactly the difference the comparison rests on.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        scope: str | None = None,
        identity: GoogleWorkloadIdentity | None = None,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._scope = scope or f"{client_id}/.default"
        self._identity = identity or GoogleWorkloadIdentity(
            timeout_s=timeout_s, transport=transport
        )
        self._timeout_s = timeout_s
        self._transport = transport
        self._cache: _CachedToken | None = None

    @property
    def mode(self) -> str:
        return "entra-fic"

    def sync_auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        raise RuntimeError("the mesh is async; use an httpx.AsyncClient")

    async def async_auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {await self._access_token()}"
        yield request

    async def _access_token(self) -> str:
        if self._cache is not None and self._cache.usable:
            return self._cache.value

        assertion = await self._identity.id_token(_ENTRA_EXCHANGE_AUDIENCE)
        boundary = f"entra federated credential (client_id={self._client_id})"
        endpoint = _ENTRA_TOKEN_URL.format(tenant=self._tenant_id)
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "scope": self._scope,
            "client_assertion_type": _CLIENT_ASSERTION_TYPE,
            "client_assertion": assertion,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.post(endpoint, data=form)
        except httpx.HTTPError as exc:
            log.error("%s -> unreachable: %s: %s", boundary, type(exc).__name__, exc)
            raise _auth_error(boundary, f"cannot reach {endpoint}: {exc}") from exc

        _log_provider_response(boundary, response)
        if not response.is_success:
            raise _auth_error(boundary, _entra_detail(response))

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise _auth_error(boundary, f"no access_token in the response: {response.text}")

        expires_in = int(payload.get("expires_in", 3600))
        self._cache = _CachedToken(token, datetime.now(UTC) + timedelta(seconds=expires_in))
        return token


def _parse_sts_credentials(body: str, boundary: str) -> _AwsCredentials:
    """Pull the credential set out of STS's XML response.

    STS's query API answers in XML whatever you ask for, so this is parsed
    rather than ``json.loads``-ed. Missing fields are raised as auth errors
    rather than ``None``-propagated, because a half-built credential fails later
    inside the signer, where the message names a signature rather than a mint.
    """
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise _auth_error(boundary, f"could not parse the STS response: {exc}") from exc

    node = root.find(".//sts:Credentials", _STS_XML_NS)
    if node is None:
        raise _auth_error(boundary, f"no Credentials element in the STS response: {body}")

    def field(name: str) -> str:
        found = node.find(f"sts:{name}", _STS_XML_NS)
        if found is None or not found.text:
            raise _auth_error(boundary, f"the STS response has no {name}")
        return found.text

    return _AwsCredentials(
        access_key_id=field("AccessKeyId"),
        secret_access_key=field("SecretAccessKey"),
        session_token=field("SessionToken"),
        expires_at=_parse_expiry(field("Expiration"), boundary),
    )


def _sts_detail(response: httpx.Response) -> str:
    """Name AWS's STS error, keeping the discriminator this leg needs.

    ``InvalidIdentityToken`` means the token could not be validated at all --
    provider setup. ``AccessDenied`` means it was validated and the trust
    conditions rejected it -- policy. The XML carries both a Code and a Message
    and the Code is the half that separates them.
    """
    try:
        root = ElementTree.fromstring(response.text)
        code = root.findtext(".//{*}Error/{*}Code") or ""
        message = root.findtext(".//{*}Error/{*}Message") or ""
    except ElementTree.ParseError:
        return f"HTTP {response.status_code}: {response.text}"

    hint = {
        "InvalidIdentityToken": (
            "the token could not be validated -- check that no explicit IAM OIDC "
            "provider exists for accounts.google.com, which breaks native federation"
        ),
        "AccessDenied": (
            "the token was read and the trust policy rejected it -- check "
            "accounts.google.com:sub against the service account's numeric unique ID"
        ),
        "ExpiredTokenException": "the minted token was already past exp when STS read it",
    }.get(code)

    detail = f"{code}: {message}" if code else f"HTTP {response.status_code}: {response.text}"
    return f"{detail} ({hint})" if hint else detail


def _entra_detail(response: httpx.Response) -> str:
    """Surface Entra's AADSTS code, which is the part that names the mismatch."""
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text}"

    code = payload.get("error", "")
    description = payload.get("error_description", response.text)

    # Matched as a whole number rather than by substring: AADSTS70021 is a
    # prefix of AADSTS700212, so `"AADSTS70021" in description` reports a
    # missing federated credential for what is actually a wrong audience --
    # sending you to check the FIC that is already correct.
    found = _AADSTS_CODE.search(description)
    hint = _AADSTS_HINTS.get(found.group(1)) if found else None

    detail = f"{code}: {description}" if code else description
    return f"{detail} ({hint})" if hint else detail


def _require(peer: str, mode: str, name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise AdapterError(
            FailureKind.VALIDATION,
            f"peer {peer} is configured {mode} but {name} is unset",
        )
    return value


def log_origin(peer: str, mode: str) -> None:
    """Record which origin served a leg, at build time.

    Both origin modules ship in one image and the modes do not overlap, so the
    mode alone identifies the origin -- but only if someone can see it. A leg
    wired to a mode for the wrong host fails at the mint with a message about a
    metadata server, which reads as an outage rather than as a misconfiguration.
    """
    log.info("leg %s -> origin=gcp mode=%s", peer, mode)


def build(peer: str, mode: str, endpoint: str) -> httpx.Auth:
    """Construct one GCP-rooted leg's credential from the per-peer environment.

    Called by ``coordinator.auth.credentials_for``; the registry stays there so
    that one function still answers "how does this leg authenticate".
    """
    prefix = peer.upper()
    log_origin(peer, mode)

    if mode == "google-id-token":
        return GoogleIdTokenAuth(
            audience=os.getenv(f"{prefix}_A2A_AUDIENCE") or _service_root(endpoint),
        )

    if mode == "aws-sigv4":
        service = os.getenv(f"{prefix}_A2A_SIGNING_SERVICE", "bedrock-agentcore")
        return AwsWebIdentitySigV4Auth(
            role_arn=_require(peer, mode, f"{prefix}_A2A_ROLE_ARN"),
            region=_require(peer, mode, f"{prefix}_A2A_REGION"),
            service=service,
            session_name=os.getenv(f"{prefix}_A2A_SESSION_NAME", "currency-mesh-master"),
            extra_headers=agentcore_headers(prefix) if service == "bedrock-agentcore" else None,
        )

    return EntraFederatedAuth(
        tenant_id=_require(peer, mode, f"{prefix}_A2A_TENANT_ID"),
        client_id=_require(peer, mode, f"{prefix}_A2A_CLIENT_ID"),
        scope=os.getenv(f"{prefix}_A2A_SCOPE") or None,
    )
