"""The outbound legs of an AWS-rooted master.

The master runs on Bedrock AgentCore Runtime, so all three legs start from the
same place -- an AWS role -- and end up in three very different conditions:

    AWS -> AWS    SigV4 with the runtime's own role       in-cloud hop, keyless
    AWS -> GCP    signed GetCallerIdentity -> GCP STS -> impersonate   keyless
    AWS -> Azure  client secret                            **not keyless**

``coordinator.auth`` holds what they share: the signer, the caches, and the
registry that dispatches to ``build`` below.

Why AWS -> GCP is keyless and AWS -> Azure is not
-------------------------------------------------
Google's Workload Identity Federation accepts an **AWS-shaped** subject token:
a SigV4-signed ``GetCallerIdentity`` request, serialised and handed over
unsent. Google replays it against AWS STS to learn who signed it. No JWT is
involved, so this leg does **not** depend on whether an AWS runtime can mint
OIDC -- which is the open question the whole topology otherwise turns on.

Entra has no equivalent. Its Federated Identity Credential wants a JWT
assertion from an issuer with OIDC discovery, and an AgentCore execution role is
not one. Outside EKS/IRSA or Cognito there is nothing for AWS to present, so
this leg falls back to a client secret and the mesh stops being secretless. That
is a measured boundary, not an implementation shortcut, and
``EntraClientSecretAuth`` is loud about it for exactly that reason.

The secret is held in AWS Secrets Manager and read with the same role that
signs the other two legs, so it is at least not a plaintext value sitting in the
runtime's configuration. That reduces the blast radius; it does not restore the
claim. One long-lived Entra credential exists either way.
"""

import json
import logging
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

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

log = logging.getLogger("coordinator.aws_origin")

#: Google requires this header on the GetCallerIdentity subject token, naming
#: the provider it is destined for, and requires it inside the signature.
_TARGET_RESOURCE_HEADER = "x-goog-cloud-target-resource"

_GOOGLE_STS_URL = "https://sts.googleapis.com/v1/token"
_IAM_CREDENTIALS_URL = (
    "https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{sa}:generateIdToken"
)
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

#: ECS-style task credentials are served here; the relative URI arrives in the
#: environment. AgentCore Runtime does *not* use this contract -- measured
#: 2026-08-12, its container environment holds only AWS_REGION.
_ECS_CREDENTIALS_HOST = "http://169.254.170.2"

#: The instance metadata service, which is what AgentCore Runtime does use.
_IMDS_HOST = "http://169.254.169.254"


class AwsWorkloadCredentials:
    """Resolves the AWS role credentials of the runtime the master is running on.

    Four sources, in the order the AWS SDKs themselves try them:

    1. ``AWS_CONTAINER_CREDENTIALS_FULL_URI`` -- the container credential
       endpoint, which is what ECS provides.
    2. ``AWS_CONTAINER_CREDENTIALS_RELATIVE_URI`` -- the ECS relative form.
    3. ``AWS_ACCESS_KEY_ID`` and friends -- Lambda, and local testing against
       the deployed mesh from an operator's shell.
    4. IMDS at ``169.254.169.254``, v2 only.

    **AgentCore Runtime uses none of the first three.** Measured 2026-08-12 by
    deploying: the container's environment holds exactly one AWS variable,
    ``AWS_REGION``. No credential endpoint, no keys. All three legs failed
    identically at this resolver, before reaching any provider.

    IMDS was originally left out on the reasoning that a silent fallback is how
    a run that should have failed loudly instead picks up an instance profile
    nobody meant to grant it. That reasoning was sound in general and wrong
    here: on this runtime there is no other source, so excluding IMDS does not
    make a wrong identity loud, it makes the right identity unreachable. The
    concern is answered instead by **logging the role name IMDS serves**, which
    makes "which identity did we pick up" an observable rather than an
    assumption.

    v2 only -- token-first. v1 is a plain GET with no token step, and falling
    back to it would paper over a runtime that had deliberately disabled it.
    """

    def __init__(
        self,
        *,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._timeout_s = timeout_s
        self._transport = transport
        self._cache: _AwsCredentials | None = None

    async def credentials(self) -> _AwsCredentials:
        if self._cache is not None and self._cache.usable:
            return self._cache
        self._cache = await self._resolve()
        return self._cache

    async def _resolve(self) -> _AwsCredentials:
        full_uri = os.getenv("AWS_CONTAINER_CREDENTIALS_FULL_URI")
        relative_uri = os.getenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")

        if full_uri:
            return await self._from_container_endpoint(full_uri)
        if relative_uri:
            return await self._from_container_endpoint(f"{_ECS_CREDENTIALS_HOST}{relative_uri}")

        key_id = os.getenv("AWS_ACCESS_KEY_ID")
        secret = os.getenv("AWS_SECRET_ACCESS_KEY")
        if key_id and secret:
            token = os.getenv("AWS_SESSION_TOKEN")
            if not token:
                log.warning(
                    "AWS_SESSION_TOKEN is unset: these look like long-lived user keys "
                    "rather than a role. The master is supposed to run under an agent "
                    "runtime's execution role."
                )
            return _AwsCredentials(
                access_key_id=key_id,
                secret_access_key=secret,
                session_token=token or "",
                # Env credentials carry no expiry we can read; re-resolving is
                # cheap and reading a stale key is not.
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )

        return await self._from_imds()

    async def _from_imds(self) -> _AwsCredentials:
        """The only source AgentCore Runtime actually offers. IMDSv2, token first.

        Three round trips: a token, the role name, then the credentials. The
        role name is logged, because it is the whole answer to "whose identity
        is this" and the reason this path is safe to take at all.
        """
        boundary = f"aws imds ({_IMDS_HOST})"
        headers_ttl = {"X-aws-ec2-metadata-token-ttl-seconds": "21600"}
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                token_response = await client.put(
                    f"{_IMDS_HOST}/latest/api/token", headers=headers_ttl
                )
                if not token_response.is_success:
                    _log_provider_response(boundary, token_response)
                    raise _auth_error(
                        boundary,
                        f"IMDSv2 token request returned {token_response.status_code}. "
                        "v1 is deliberately not attempted: falling back to it would "
                        "paper over a runtime that had turned v2 on for a reason.",
                    )
                headers = {"X-aws-ec2-metadata-token": token_response.text.strip()}

                role_response = await client.get(
                    f"{_IMDS_HOST}/latest/meta-data/iam/security-credentials/",
                    headers=headers,
                )
                _log_provider_response(boundary, role_response)
                if not role_response.is_success:
                    raise _auth_error(
                        boundary,
                        f"no role is attached to this runtime "
                        f"({role_response.status_code}: {role_response.text})",
                    )
                role = role_response.text.strip().splitlines()[0]

                credentials_response = await client.get(
                    f"{_IMDS_HOST}/latest/meta-data/iam/security-credentials/{role}",
                    headers=headers,
                )
        except httpx.HTTPError as exc:
            log.error("%s -> unreachable: %s: %s", boundary, type(exc).__name__, exc)
            raise _auth_error(
                boundary,
                f"no AWS role credentials anywhere: the environment holds no container "
                f"credential endpoint and no keys, and IMDS is unreachable ({exc}). The "
                "master must run on an AWS runtime with a role attached.",
            ) from exc

        _log_provider_response(boundary, credentials_response)
        if not credentials_response.is_success:
            raise _auth_error(
                boundary,
                f"{credentials_response.status_code}: {credentials_response.text}",
            )

        payload = credentials_response.json()
        # Named, once per refresh. The identity every leg signs with should not
        # have to be inferred from a denial.
        log.info("resolved credentials from IMDS for role %s", role)
        try:
            return _AwsCredentials(
                access_key_id=payload["AccessKeyId"],
                secret_access_key=payload["SecretAccessKey"],
                session_token=payload["Token"],
                expires_at=_parse_expiry(payload["Expiration"], boundary),
            )
        except KeyError as exc:
            raise _auth_error(
                boundary, f"IMDS credentials are missing {exc.args[0]}: {credentials_response.text}"
            ) from exc

    async def _from_container_endpoint(self, url: str) -> _AwsCredentials:
        boundary = f"aws container credentials ({url})"
        headers = {}
        # Required whenever a full URI is used, by both ECS agent v1.4+ and
        # AgentCore. Two spellings, because the variable was renamed and both
        # are still populated depending on the runtime.
        auth_token = os.getenv("AWS_CONTAINER_AUTHORIZATION_TOKEN")
        if not auth_token and (path := os.getenv("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE")):
            try:
                with open(path) as handle:
                    auth_token = handle.read().strip()
            except OSError as exc:
                raise _auth_error(
                    boundary,
                    f"AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE={path} is unreadable: {exc}",
                ) from exc
        if auth_token:
            headers["Authorization"] = auth_token

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            log.error("%s -> unreachable: %s: %s", boundary, type(exc).__name__, exc)
            raise _auth_error(boundary, f"cannot reach the credentials endpoint: {exc}") from exc

        _log_provider_response(boundary, response)
        if not response.is_success:
            raise _auth_error(boundary, f"{response.status_code}: {response.text}")

        payload = response.json()
        try:
            return _AwsCredentials(
                access_key_id=payload["AccessKeyId"],
                secret_access_key=payload["SecretAccessKey"],
                session_token=payload["Token"],
                expires_at=_parse_expiry(payload["Expiration"], boundary),
            )
        except KeyError as exc:
            raise _auth_error(
                boundary, f"credentials response is missing {exc.args[0]}: {response.text}"
            ) from exc


class AwsRoleSigV4Auth(httpx.Auth):
    """AWS -> AWS. The master's own role, signing a call to a sibling runtime.

    The cheapest leg in the mesh and the least interesting, which is the point
    of keeping it labelled: **this is an in-cloud hop.** The master and the AWS
    agent are both AgentCore runtimes in one account, so nothing here crosses a
    vendor boundary and this cell must not pad the interop claim. The matrix
    marks it; ``CURRENCY_COORDINATOR_CLOUD=aws`` is what tells it to.

    It is also the control case for the seam: if this leg fails, the signer is
    wrong, not the federation.

    Note what is *absent* compared to the Cloud-Run-rooted version of this leg:
    there is no token exchange at all. That one minted a Google OIDC token,
    presented it to STS ``AssumeRoleWithWebIdentity``, and signed with the
    result. This one signs with credentials the runtime already has.
    """

    #: SigV4 signs a hash of the body, so httpx must materialise it first.
    requires_request_body = True

    def __init__(
        self,
        *,
        region: str,
        service: str = "bedrock-agentcore",
        extra_headers: dict[str, str] | None = None,
        credentials: AwsWorkloadCredentials | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._region = region
        self._service = service
        self._extra_headers = extra_headers or {}
        self._credentials = credentials or AwsWorkloadCredentials(transport=transport)

    @property
    def mode(self) -> str:
        return "aws-sigv4-role"

    def sync_auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        raise RuntimeError("the mesh is async; use an httpx.AsyncClient")

    async def async_auth_flow(self, request: httpx.Request):
        credentials = await self._credentials.credentials()
        # Set before signing and named to the signer, so they fall inside the
        # signature. AgentCore's session header is required on every request
        # including the card fetch, and an unsigned x-amzn-* header is the kind
        # of thing a service may accept today and reject later.
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


class GcpFederatedIdTokenAuth(httpx.Auth):
    """AWS -> GCP. Keyless, and notably without minting a JWT anywhere.

    Four steps, of which only the last two leave the process::

        1. resolve the AWS role credentials
        2. build and sign a GetCallerIdentity request -- and do not send it
        3. hand that to Google STS, which replays it against AWS to learn the caller
        4. impersonate the target service account for an ID token

    Step 2 is the part worth understanding: the *subject token* is a signed HTTP
    request, serialised as JSON and never issued by us. Google issues it. That
    is why this leg works from a runtime that cannot mint OIDC at all, and it is
    the asymmetry that makes AWS -> GCP cheap while AWS -> Azure is not.

    Step 4 exists because Cloud Run validates an **ID token** whose audience is
    its own service URL, and the STS exchange yields an *access* token. The
    federated principal therefore needs ``roles/iam.serviceAccountTokenCreator``
    on the service account it impersonates -- a grant that is easy to forget and
    which denies with a 403 naming the *service account*, not the pool.

    Cost, relative to the GCP-rooted equivalent this replaced: that leg was one
    hop to the metadata server. This is two network round trips before the call,
    plus a local signature.
    """

    def __init__(
        self,
        *,
        audience: str,
        pool_provider: str,
        service_account: str,
        region: str,
        credentials: AwsWorkloadCredentials | None = None,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._audience = audience
        self._pool_provider = pool_provider
        self._service_account = service_account
        self._region = region
        self._credentials = credentials or AwsWorkloadCredentials(transport=transport)
        self._timeout_s = timeout_s
        self._transport = transport
        self._cache: _CachedToken | None = None

    @property
    def mode(self) -> str:
        return "gcp-wif-aws"

    def sync_auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        raise RuntimeError("the mesh is async; use an httpx.AsyncClient")

    async def async_auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {await self._id_token()}"
        yield request

    async def _id_token(self) -> str:
        if self._cache is not None and self._cache.usable:
            return self._cache.value

        federated = await self._federated_access_token()
        token = await self._impersonated_id_token(federated)
        # generateIdToken returns no expiry; the ID tokens it mints last an
        # hour, and the skew in _CachedToken keeps us clear of the edge.
        self._cache = _CachedToken(token, datetime.now(UTC) + timedelta(seconds=3600))
        return token

    async def _federated_access_token(self) -> str:
        subject_token = await self._subject_token()
        boundary = f"google sts token exchange (provider={self._pool_provider})"
        form = {
            "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
            "audience": self._pool_provider,
            "scope": _CLOUD_PLATFORM_SCOPE,
            "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
            "subject_token": subject_token,
            "subject_token_type": "urn:ietf:params:aws:token-type:aws4_request",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.post(_GOOGLE_STS_URL, data=form)
        except httpx.HTTPError as exc:
            log.error("%s -> unreachable: %s: %s", boundary, type(exc).__name__, exc)
            raise _auth_error(boundary, f"cannot reach {_GOOGLE_STS_URL}: {exc}") from exc

        _log_provider_response(boundary, response)
        if not response.is_success:
            raise _auth_error(boundary, _google_sts_detail(response))

        token = response.json().get("access_token")
        if not token:
            raise _auth_error(boundary, f"no access_token in the response: {response.text}")
        return token

    async def _subject_token(self) -> str:
        """Build the signed-but-unsent ``GetCallerIdentity`` Google will replay.

        The serialisation is Google's, not AWS's: a JSON object of url, method
        and a *list* of key/value header pairs, URL-encoded whole. The header
        list must include the signed ``x-goog-cloud-target-resource``, or the
        exchange is refused with a message that does not mention it.
        """
        credentials = await self._credentials.credentials()
        url = (
            f"https://sts.{self._region}.amazonaws.com/"
            "?Action=GetCallerIdentity&Version=2011-06-15"
        )
        request = httpx.Request("POST", url)
        request.headers[_TARGET_RESOURCE_HEADER] = self._pool_provider
        _sign_request(
            request,
            credentials=credentials,
            region=self._region,
            service="sts",
            now=datetime.now(UTC),
            extra_signed_headers=(_TARGET_RESOURCE_HEADER,),
        )

        # Built by name rather than by iterating the request's headers, and the
        # capital A on Authorization is load-bearing. **httpx lowercases header
        # names**, Google matches them case-sensitively, and the whole exchange
        # is then refused with `invalid_grant: The given AWS request doesn't
        # contain all the required headers` -- a message that names no header
        # and reads as though one were missing when every one was present.
        # Measured 2026-08-12 against the live endpoint.
        #
        # Iterating also swept in httpx's own `content-length: 0`, which is not
        # part of what was signed. Google's reference client sends exactly the
        # five below and nothing else, so send exactly those.
        headers = [
            {"key": "Authorization", "value": request.headers["authorization"]},
            {"key": "host", "value": request.headers["host"]},
            {"key": "x-amz-date", "value": request.headers["x-amz-date"]},
            {"key": _TARGET_RESOURCE_HEADER, "value": self._pool_provider},
        ]
        if "x-amz-security-token" in request.headers:
            headers.append(
                {"key": "x-amz-security-token", "value": request.headers["x-amz-security-token"]}
            )

        payload = {"url": url, "method": "POST", "headers": headers}
        return quote(json.dumps(payload))

    async def _impersonated_id_token(self, federated_token: str) -> str:
        boundary = f"iamcredentials generateIdToken (sa={self._service_account})"
        url = _IAM_CREDENTIALS_URL.format(sa=self._service_account)
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {federated_token}"},
                    json={"audience": self._audience, "includeEmail": True},
                )
        except httpx.HTTPError as exc:
            log.error("%s -> unreachable: %s: %s", boundary, type(exc).__name__, exc)
            raise _auth_error(boundary, f"cannot reach {url}: {exc}") from exc

        _log_provider_response(boundary, response)
        if not response.is_success:
            raise _auth_error(boundary, _impersonation_detail(response, self._service_account))

        token = response.json().get("token")
        if not token:
            raise _auth_error(boundary, f"no token in the response: {response.text}")
        return token


def _google_sts_detail(response: httpx.Response) -> str:
    """Name Google's STS error, with the discriminator this leg actually needs.

    The mirror of the AWS STS discriminator this project relies on elsewhere:
    ``invalid_grant`` means the subject token itself was not accepted,
    ``invalid_request`` or ``permission_denied`` means it was read and the
    pool's attribute condition rejected the principal. Provider-setup bug versus
    condition bug -- different afternoons.
    """
    try:
        payload = response.json()
    except ValueError:
        return f"{response.status_code}: {response.text}"

    code = payload.get("error", "unknown_error")
    description = payload.get("error_description", response.text)
    hint = {
        "invalid_grant": (
            "the GetCallerIdentity subject token was rejected outright, before any "
            "identity was read. If it says 'doesn't contain all the required headers', "
            "check the *casing*: Google matches 'Authorization' case-sensitively and "
            "httpx lowercases header names, so a complete header list is refused as an "
            "incomplete one (measured 2026-08-12). Otherwise check that the provider's "
            f"AWS account ID matches the role in use and that {_TARGET_RESOURCE_HEADER} "
            "was inside the signature rather than merely present"
        ),
        "invalid_request": (
            "the token was read but the request was malformed -- most often the "
            "subject_token is not URL-encoded, or the header list disagrees with "
            "what the signature covered"
        ),
        # Measured 2026-08-12: this is the code the attribute condition returns,
        # not permission_denied. Both are mapped because the distinction that
        # matters is condition-vs-token, and only the message says which.
        "unauthorized_client": (
            "the caller WAS identified and the pool's attribute condition rejected it "
            "-- so the token itself is fine. Check attribute.aws_role against the "
            "assumed-role ARN (arn:aws:sts::<acct>:assumed-role/<role>), which is not "
            "the same string as the role ARN you granted"
        ),
        "permission_denied": (
            "the caller was identified and the pool's attribute condition did not "
            "match it -- check attribute.aws_role against the assumed-role ARN, "
            "which is not the same string as the role ARN you granted"
        ),
    }.get(code)
    detail = f"{response.status_code} {code}: {description}"
    return f"{detail} [{hint}]" if hint else detail


def _impersonation_detail(response: httpx.Response, service_account: str) -> str:
    """A 403 here names the service account, not the federation, and misleads."""
    try:
        payload = response.json()
        message = payload.get("error", {}).get("message", response.text)
    except ValueError:
        message = response.text

    if response.status_code in (401, 403):
        return (
            f"{response.status_code}: {message} [the federated principal needs "
            f"roles/iam.serviceAccountTokenCreator on {service_account}. This denial "
            "is about impersonation, not about the workload identity pool -- the STS "
            "exchange before it already succeeded.]"
        )
    return f"{response.status_code}: {message}"


class SecretsManagerValue:
    """Reads one secret with the master's own role, at first use rather than start.

    Exists so the Entra client secret is not a plaintext value in the runtime's
    configuration, where ``get-agent-runtime`` would print it to anyone who can
    describe the runtime. It is signed with the same credentials as the AWS leg,
    so no new trust relationship is involved -- just a resource policy.

    Lazy on purpose: a master whose Azure leg is not configured should not fail
    to start, and the two keyless legs must not be blocked on a secret they do
    not use.
    """

    def __init__(
        self,
        secret_arn: str,
        *,
        region: str,
        json_key: str | None = None,
        credentials: AwsWorkloadCredentials | None = None,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._secret_arn = secret_arn
        self._region = region
        self._json_key = json_key
        self._credentials = credentials or AwsWorkloadCredentials(transport=transport)
        self._timeout_s = timeout_s
        self._transport = transport
        self._cache: str | None = None

    async def value(self) -> str:
        if self._cache is not None:
            return self._cache

        boundary = f"aws secretsmanager GetSecretValue (secret={self._secret_arn})"
        url = f"https://secretsmanager.{self._region}.amazonaws.com/"
        target = "secretsmanager.GetSecretValue"
        request = httpx.Request(
            "POST",
            url,
            content=json.dumps({"SecretId": self._secret_arn}).encode(),
            headers={"content-type": "application/x-amz-json-1.1", "x-amz-target": target},
        )
        _sign_request(
            request,
            credentials=await self._credentials.credentials(),
            region=self._region,
            service="secretsmanager",
            now=datetime.now(UTC),
            extra_signed_headers=("x-amz-target",),
        )

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.send(request)
        except httpx.HTTPError as exc:
            log.error("%s -> unreachable: %s: %s", boundary, type(exc).__name__, exc)
            raise _auth_error(boundary, f"cannot reach {url}: {exc}") from exc

        _log_provider_response(boundary, response)
        if not response.is_success:
            raise _auth_error(boundary, f"{response.status_code}: {response.text}")

        payload = response.json()
        secret = payload.get("SecretString")
        if not secret:
            raise _auth_error(
                boundary,
                "the secret has no SecretString. A binary secret is not supported here; "
                "store the Entra client secret as a string.",
            )

        # Stored either as the bare secret or as a JSON object with a named
        # field. Both are common and guessing wrong yields an Entra
        # AADSTS7000215 that says only "invalid client secret", which reads as a
        # rotation problem rather than a parsing one.
        if self._json_key:
            try:
                secret = json.loads(secret)[self._json_key]
            except (ValueError, KeyError, TypeError) as exc:
                raise _auth_error(
                    boundary,
                    f"could not read key {self._json_key!r} out of the secret: {exc}",
                ) from exc

        self._cache = secret
        return secret


class EntraClientSecretAuth(httpx.Auth):
    """AWS -> Azure. **This leg is not keyless, and that is the finding.**

    Entra's Federated Identity Credential needs a JWT assertion from an issuer
    it can discover. An AgentCore execution role is not an OIDC issuer, and AWS
    will not mint a token for an arbitrary audience outside EKS/IRSA or Cognito.
    There is nothing to federate *with*, so an AWS-rooted master falls back to a
    client secret.

    This class exists to make the boundary measurable rather than to make the
    mesh work. It logs a warning on construction because a silent fallback is
    exactly how "we deployed a secretless mesh" gets written about a mesh with a
    secret in it -- and ``MeshRun.auth_modes`` reports ``entra-client-secret``,
    which must never be mistaken for the ``entra-fic`` this replaced.

    If AWS ever gives ordinary compute an OIDC issuer, this whole class is
    deleted and the federated path comes back.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        scope: str,
        client_secret: str | None = None,
        secret_source: SecretsManagerValue | None = None,
        timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not client_secret and secret_source is None:
            raise AdapterError(
                FailureKind.VALIDATION,
                "entra-client-secret needs either AZURE_A2A_CLIENT_SECRET or "
                "AZURE_A2A_CLIENT_SECRET_ARN",
            )
        self._tenant_id = tenant_id
        self._client_id = client_id
        self._client_secret = client_secret
        self._secret_source = secret_source
        self._scope = scope
        self._timeout_s = timeout_s
        self._transport = transport
        self._cache: _CachedToken | None = None
        log.warning(
            "AWS -> Azure is using a client secret (client_id=%s, source=%s). This leg "
            "is NOT keyless: Entra requires a JWT assertion and no AgentCore execution "
            "role can mint one. Any claim of a secretless mesh must exclude it.",
            client_id,
            "secretsmanager" if secret_source is not None else "environment",
        )

    @property
    def mode(self) -> str:
        return "entra-client-secret"

    def sync_auth_flow(self, request: httpx.Request) -> Iterator[httpx.Request]:
        raise RuntimeError("the mesh is async; use an httpx.AsyncClient")

    async def async_auth_flow(self, request: httpx.Request):
        request.headers["Authorization"] = f"Bearer {await self._access_token()}"
        yield request

    async def _secret(self) -> str:
        if self._client_secret:
            return self._client_secret
        assert self._secret_source is not None  # guaranteed by __init__
        self._client_secret = await self._secret_source.value()
        return self._client_secret

    async def _access_token(self) -> str:
        if self._cache is not None and self._cache.usable:
            return self._cache.value

        boundary = f"entra client credentials (tenant={self._tenant_id}, client={self._client_id})"
        url = f"https://login.microsoftonline.com/{self._tenant_id}/oauth2/v2.0/token"
        form = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": await self._secret(),
            "scope": self._scope,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = await client.post(url, data=form)
        except httpx.HTTPError as exc:
            log.error("%s -> unreachable: %s: %s", boundary, type(exc).__name__, exc)
            raise _auth_error(boundary, f"cannot reach {url}: {exc}") from exc

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


def _entra_detail(response: httpx.Response) -> str:
    """Surface Entra's AADSTS code, which is the part that names the mismatch."""
    try:
        payload = response.json()
    except ValueError:
        return f"{response.status_code}: {response.text}"
    code = payload.get("error", "unknown_error")
    description = payload.get("error_description", response.text)
    return f"{response.status_code} {code}: {description}"


def _require(peer: str, mode: str, name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise AdapterError(
            FailureKind.VALIDATION,
            f"{peer} is configured with {name.rsplit('_', 1)[0]}"
            f"_A2A_AUTH={mode} but {name} is unset",
        )
    return value


def _region(peer: str, mode: str, prefix: str) -> str:
    """The region to sign in, defaulting to the one the master is running in.

    Every AWS runtime sets ``AWS_REGION``, so requiring the per-peer variable
    would be asking the operator to restate a fact the environment already
    holds -- and a restated fact is one that can disagree.
    """
    region = os.getenv(f"{prefix}_A2A_REGION") or os.getenv("AWS_REGION")
    if not region:
        raise AdapterError(
            FailureKind.VALIDATION,
            f"{peer} is configured with {prefix}_A2A_AUTH={mode} but neither "
            f"{prefix}_A2A_REGION nor AWS_REGION is set",
        )
    return region


def build(peer: str, mode: str, endpoint: str) -> httpx.Auth:
    """Construct one leg's credential from the per-peer environment.

    Called by ``coordinator.auth.credentials_for``; the registry stays there so
    that one function still answers "how does this leg authenticate".
    """
    prefix = peer.upper()

    if mode == "aws-sigv4-role":
        service = os.getenv(f"{prefix}_A2A_SIGNING_SERVICE", "bedrock-agentcore")
        return AwsRoleSigV4Auth(
            region=_region(peer, mode, prefix),
            service=service,
            extra_headers=agentcore_headers(prefix) if service == "bedrock-agentcore" else None,
        )

    if mode == "gcp-wif-aws":
        return GcpFederatedIdTokenAuth(
            audience=os.getenv(f"{prefix}_A2A_AUDIENCE") or _service_root(endpoint),
            pool_provider=_require(peer, mode, f"{prefix}_A2A_POOL_PROVIDER"),
            service_account=_require(peer, mode, f"{prefix}_A2A_SERVICE_ACCOUNT"),
            # The region of the STS endpoint the subject token is signed
            # against, which is the master's own -- not a property of GCP.
            region=_region(peer, mode, prefix),
        )

    client_id = _require(peer, mode, f"{prefix}_A2A_CLIENT_ID")
    secret_arn = os.getenv(f"{prefix}_A2A_CLIENT_SECRET_ARN")
    secret_source = None
    if secret_arn:
        secret_source = SecretsManagerValue(
            secret_arn,
            region=_region(peer, mode, prefix),
            json_key=os.getenv(f"{prefix}_A2A_CLIENT_SECRET_KEY") or None,
        )
    return EntraClientSecretAuth(
        tenant_id=_require(peer, mode, f"{prefix}_A2A_TENANT_ID"),
        client_id=client_id,
        client_secret=os.getenv(f"{prefix}_A2A_CLIENT_SECRET") or None,
        secret_source=secret_source,
        scope=os.getenv(f"{prefix}_A2A_SCOPE") or f"{client_id}/.default",
    )
