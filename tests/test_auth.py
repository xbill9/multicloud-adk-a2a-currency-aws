"""Cloud-free tests of the cross-cloud credential seam.

Every provider is a stubbed transport, so this suite asserts the things that
actually went wrong in the predecessor series -- the shape of the exchange,
which condition a denial names, whether the error survives the trip back --
without an account, a network, or a vendor SDK.

The master is rooted in AWS, so all three legs start from an AWS role. What the
suite cannot assert is that any real provider accepts any of it.
"""

import json
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

import httpx
import pytest

from coordinator.auth import (
    AGENTCORE_SESSION_HEADER,
    _AwsCredentials,
    _CachedToken,
    _EXPIRY_SKEW,
    _parse_expiry,
    _signing_key,
    auth_mode,
    credentials_for,
    is_keyless,
)
from coordinator.aws_origin import (
    AwsRoleSigV4Auth,
    AwsWorkloadCredentials,
    EntraClientSecretAuth,
    GcpFederatedIdTokenAuth,
    SecretsManagerValue,
)
from coordinator.errors import AdapterError, FailureKind


def transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.fixture
def role_env(monkeypatch):
    """The environment an AWS runtime hands its container, in the simplest form."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ASIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "FQoEXAMPLEtoken")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    for name in (
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    ):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------------------
# Resolving the runtime's own role -- every leg starts here
# --------------------------------------------------------------------------


async def test_container_endpoint_is_preferred_over_environment_keys(monkeypatch):
    """The order the AWS SDKs themselves use. Getting it backwards would sign
    with whatever stale keys happened to be in the environment."""
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://169.254.170.2/creds")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIASTALE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "stale")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "AccessKeyId": "ASIAFROMENDPOINT",
                "SecretAccessKey": "secret",
                "Token": "token",
                "Expiration": "2099-01-01T00:00:00Z",
            },
        )

    credentials = await AwsWorkloadCredentials(transport=transport(handler)).credentials()

    assert credentials.access_key_id == "ASIAFROMENDPOINT"


async def test_container_endpoint_sends_the_authorization_token(monkeypatch):
    """Required by both the ECS agent and AgentCore when a full URI is used."""
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://169.254.170.2/creds")
    monkeypatch.setenv("AWS_CONTAINER_AUTHORIZATION_TOKEN", "opaque-token")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "AccessKeyId": "k",
                "SecretAccessKey": "s",
                "Token": "t",
                "Expiration": "2099-01-01T00:00:00Z",
            },
        )

    await AwsWorkloadCredentials(transport=transport(handler)).credentials()

    assert seen[0].headers["Authorization"] == "opaque-token"


async def test_the_token_file_form_is_read_from_disk(monkeypatch, tmp_path):
    """The newer spelling. Missing it produces a 401 from a local endpoint,
    which reads like a network fault rather than a missing header."""
    token_file = tmp_path / "token"
    token_file.write_text("from-file\n")
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://169.254.170.2/creds")
    monkeypatch.delenv("AWS_CONTAINER_AUTHORIZATION_TOKEN", raising=False)
    monkeypatch.setenv("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", str(token_file))
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "AccessKeyId": "k",
                "SecretAccessKey": "s",
                "Token": "t",
                "Expiration": "2099-01-01T00:00:00Z",
            },
        )

    await AwsWorkloadCredentials(transport=transport(handler)).credentials()

    assert seen[0].headers["Authorization"] == "from-file"


@pytest.fixture
def bare_env(monkeypatch):
    """AgentCore Runtime's actual environment, measured 2026-08-12: AWS_REGION
    and nothing else. No credential endpoint, no keys."""
    for name in (
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_REGION", "us-west-2")


def imds(
    *,
    token_status: int = 200,
    role_status: int = 200,
    role: str = "currency-master-agentcore-exec",
    credentials_status: int = 200,
    record: list | None = None,
) -> AwsWorkloadCredentials:
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        path = request.url.path
        if path == "/latest/api/token":
            return httpx.Response(token_status, text="imds-token")
        if path == "/latest/meta-data/iam/security-credentials/":
            return httpx.Response(role_status, text=role)
        if path == f"/latest/meta-data/iam/security-credentials/{role}":
            return httpx.Response(
                credentials_status,
                json={
                    "AccessKeyId": "ASIAFROMIMDS",
                    "SecretAccessKey": "secret",
                    "Token": "token",
                    "Expiration": "2099-01-01T00:00:00Z",
                },
            )
        raise AssertionError(f"unexpected IMDS path: {path}")

    return AwsWorkloadCredentials(transport=transport(handler))


async def test_agentcore_credentials_come_from_imds(bare_env):
    """The measured shape of the deployed runtime. Without this path every leg
    fails at the resolver, before reaching any provider -- which is what the
    first deployed run did."""
    credentials = await imds().credentials()

    assert credentials.access_key_id == "ASIAFROMIMDS"


async def test_imds_is_v2_token_first(bare_env):
    """v1 is a plain GET with no token step. Falling back to it would paper over
    a runtime that had turned v2 on deliberately."""
    seen: list[httpx.Request] = []
    await imds(record=seen).credentials()

    assert seen[0].method == "PUT"
    assert seen[0].url.path == "/latest/api/token"
    assert seen[0].headers["X-aws-ec2-metadata-token-ttl-seconds"] == "21600"
    assert all(r.headers.get("X-aws-ec2-metadata-token") == "imds-token" for r in seen[1:])


async def test_the_role_imds_serves_is_logged_by_name(bare_env, caplog):
    """The whole reason this path is safe to take: "which identity did we pick
    up" has to be an observable, not an assumption."""
    with caplog.at_level(logging.INFO, logger="coordinator.aws_origin"):
        await imds().credentials()

    assert "currency-master-agentcore-exec" in caplog.text


async def test_a_v2_refusal_is_not_retried_as_v1(bare_env):
    seen: list[httpx.Request] = []

    with pytest.raises(AdapterError) as exc:
        await imds(token_status=403, record=seen).credentials()

    assert "v1 is deliberately not attempted" in str(exc.value)
    assert len(seen) == 1


async def test_no_role_attached_says_so(bare_env):
    with pytest.raises(AdapterError) as exc:
        await imds(role_status=404).credentials()

    assert "no role is attached" in str(exc.value)


async def test_unreachable_imds_names_every_source_it_looked_for(bare_env):
    """The last-resort failure. It has to enumerate what was tried, because on a
    runtime with none of them the message is the entire diagnosis."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host", request=request)

    with pytest.raises(AdapterError) as exc:
        await AwsWorkloadCredentials(transport=transport(handler)).credentials()

    message = str(exc.value)
    assert exc.value.kind is FailureKind.AUTHENTICATION
    assert "no container credential endpoint and no keys" in message
    assert "IMDS is unreachable" in message


async def test_long_lived_user_keys_are_warned_about(monkeypatch, caplog):
    """No session token means these are not a role, and the master is supposed
    to run under one."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAUSER")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", raising=False)
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", raising=False)

    with caplog.at_level(logging.WARNING, logger="coordinator.aws_origin"):
        await AwsWorkloadCredentials().credentials()

    assert "long-lived user keys" in caplog.text


# --------------------------------------------------------------------------
# AWS -> AWS, the in-cloud hop
# --------------------------------------------------------------------------


async def test_role_sigv4_signs_with_the_runtimes_own_credentials(role_env):
    auth = AwsRoleSigV4Auth(region="us-west-2")
    request = httpx.Request(
        "POST", "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/abc/invocations/",
        json={"q": 1},
    )

    async for signed in auth.async_auth_flow(request):
        header = signed.headers["Authorization"]
        assert header.startswith("AWS4-HMAC-SHA256 Credential=ASIAEXAMPLE/")
        assert "/us-west-2/bedrock-agentcore/aws4_request" in header
        # Sent *and* signed. Omitting the session token from SignedHeaders is a
        # signature mismatch that reads as a clock problem.
        assert "x-amz-security-token" in header
        assert signed.headers["x-amz-security-token"] == "FQoEXAMPLEtoken"


async def test_the_agentcore_session_header_falls_inside_the_signature(role_env, monkeypatch):
    """AgentCore requires it on every request including the card fetch. An
    unsigned x-amzn-* header is the kind of thing a service accepts today and
    rejects later."""
    monkeypatch.setenv("AWS_A2A_AUTH", "aws-sigv4-role")
    monkeypatch.setenv("AWS_A2A_SESSION_ID", "a" * 40)
    auth = credentials_for("aws", "https://bedrock-agentcore.us-west-2.amazonaws.com/x")

    async for signed in auth.async_auth_flow(httpx.Request("POST", "https://x.example/a2a")):
        assert signed.headers[AGENTCORE_SESSION_HEADER] == "a" * 40
        assert AGENTCORE_SESSION_HEADER.lower() in signed.headers["Authorization"]


async def test_a_short_session_id_is_refused_before_the_call(role_env, monkeypatch):
    """AgentCore rejects anything under 33 characters, with an error about the
    request rather than about the header."""
    monkeypatch.setenv("AWS_A2A_AUTH", "aws-sigv4-role")
    monkeypatch.setenv("AWS_A2A_SESSION_ID", "short")

    with pytest.raises(AdapterError) as exc:
        credentials_for("aws", "https://agentcore.example")

    assert exc.value.kind is FailureKind.VALIDATION
    assert "33 characters" in str(exc.value)


async def test_signature_covers_the_request_body(role_env):
    """requires_request_body is not decoration: two bodies must not share a signature."""
    url = "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/abc/invocations/"
    signatures = []
    for body in ({"amount": 100}, {"amount": 999}):
        auth = AwsRoleSigV4Auth(region="us-west-2", extra_headers={})
        async for signed in auth.async_auth_flow(httpx.Request("POST", url, json=body)):
            signatures.append(signed.headers["Authorization"].split("Signature=")[1])

    assert signatures[0] != signatures[1]


def test_signing_key_matches_the_published_aws_vector():
    """The derivation from AWS's own SigV4 documentation, verbatim."""
    key = _signing_key(
        "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY", "20150830", "us-east-1", "iam"
    )

    assert key.hex() == "c4afb1cc5771d871763a393e44b703571b55cc28424d1a5e86da6ed3c154a4b9"


# --------------------------------------------------------------------------
# AWS -> GCP, keyless without a JWT anywhere
# --------------------------------------------------------------------------


def gcp_auth(
    *,
    sts_payload: dict | None = None,
    sts_status: int = 200,
    iam_payload: dict | None = None,
    iam_status: int = 200,
    record: list | None = None,
) -> GcpFederatedIdTokenAuth:
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        if "sts.googleapis.com" in str(request.url):
            return httpx.Response(
                sts_status, json=sts_payload or {"access_token": "federated-access-token"}
            )
        if "iamcredentials.googleapis.com" in str(request.url):
            return httpx.Response(iam_status, json=iam_payload or {"token": "gcp-id-token"})
        raise AssertionError(f"unexpected host: {request.url}")

    return GcpFederatedIdTokenAuth(
        audience="https://currency-gcp.a.run.app",
        pool_provider="//iam.googleapis.com/projects/1/locations/global/workloadIdentityPools/p/providers/aws",
        service_account="currency-master@example.iam.gserviceaccount.com",
        region="us-west-2",
        transport=transport(handler),
    )


async def test_the_subject_token_is_a_signed_request_that_is_never_sent(role_env):
    """The mechanism worth understanding: Google issues the GetCallerIdentity
    request, not us. That is why this leg works from a runtime that cannot mint
    OIDC at all."""
    seen: list[httpx.Request] = []
    auth = gcp_auth(record=seen)

    async for _ in auth.async_auth_flow(httpx.Request("POST", "https://gcp.example/a2a")):
        pass

    # Nothing was sent to AWS STS.
    assert not [r for r in seen if "amazonaws.com" in str(r.url)]

    exchange = next(r for r in seen if "sts.googleapis.com" in str(r.url))
    body = exchange.content.decode()
    assert "subject_token_type=urn%3Aietf%3Aparams%3Aaws%3Atoken-type%3Aaws4_request" in body

    subject = next(
        part.split("=", 1)[1] for part in body.split("&") if part.startswith("subject_token=")
    )
    payload = json.loads(unquote(unquote(subject)))
    assert "Action=GetCallerIdentity" in payload["url"]
    headers = {item["key"].lower(): item["value"] for item in payload["headers"]}
    # Present *and* signed. Google refuses an exchange whose header list
    # disagrees with the signature, with a message that does not mention it.
    assert headers["x-goog-cloud-target-resource"] == auth._pool_provider
    assert "x-goog-cloud-target-resource" in headers["authorization"]


async def test_the_subject_token_header_names_are_cased_the_way_google_reads_them(role_env):
    """Measured 2026-08-12 against the live endpoint: Google matches
    `Authorization` case-sensitively, and httpx lowercases header names. The
    complete-but-lowercased list came back as `invalid_grant: The given AWS
    request doesn't contain all the required headers` -- a message that names no
    header and reads as though one were missing.

    Also asserts nothing extra rides along: iterating httpx's headers swept in
    `content-length: 0`, which was never part of what was signed.
    """
    seen: list[httpx.Request] = []
    auth = gcp_auth(record=seen)

    async for _ in auth.async_auth_flow(httpx.Request("POST", "https://gcp.example/a2a")):
        pass

    body = next(r for r in seen if "sts.googleapis.com" in str(r.url)).content.decode()
    subject = next(
        part.split("=", 1)[1] for part in body.split("&") if part.startswith("subject_token=")
    )
    keys = [item["key"] for item in json.loads(unquote(unquote(subject)))["headers"]]

    assert "Authorization" in keys
    assert "authorization" not in keys
    assert set(keys) == {
        "Authorization",
        "host",
        "x-amz-date",
        "x-amz-security-token",
        "x-goog-cloud-target-resource",
    }


async def test_an_attribute_condition_rejection_says_the_token_was_fine(role_env):
    """The code is `unauthorized_client`, measured -- not `permission_denied`.
    It means the caller was identified and the condition refused it, which is a
    completely different fix from a malformed token."""
    auth = gcp_auth(
        sts_payload={
            "error": "unauthorized_client",
            "error_description": "The given credential is rejected by the attribute condition.",
        },
        sts_status=400,
    )

    with pytest.raises(AdapterError) as exc:
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://gcp.example/a2a")):
            pass

    message = str(exc.value)
    assert "WAS identified" in message
    assert "assumed-role" in message


async def test_the_impersonated_id_token_is_the_bearer(role_env):
    """Cloud Run validates an ID token whose audience is its own URL, and the
    STS exchange yields an *access* token. Presenting the wrong one 401s."""
    auth = gcp_auth()

    async for signed in auth.async_auth_flow(httpx.Request("POST", "https://gcp.example/a2a")):
        assert signed.headers["Authorization"] == "Bearer gcp-id-token"


async def test_the_impersonation_call_asks_for_the_service_url_audience(role_env):
    seen: list[httpx.Request] = []
    auth = gcp_auth(record=seen)

    async for _ in auth.async_auth_flow(httpx.Request("POST", "https://gcp.example/a2a")):
        pass

    impersonation = next(r for r in seen if "iamcredentials" in str(r.url))
    assert json.loads(impersonation.content)["audience"] == "https://currency-gcp.a.run.app"


async def test_permission_denied_points_at_the_attribute_condition(role_env):
    """The pool read the caller and its condition rejected it -- a condition
    bug, not a provider-setup bug. Distinguishing those is the whole value of
    this message."""
    auth = gcp_auth(
        sts_payload={"error": "permission_denied", "error_description": "nope"}, sts_status=403
    )

    with pytest.raises(AdapterError) as exc:
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://gcp.example/a2a")):
            pass

    message = str(exc.value)
    assert "permission_denied" in message
    # The trap: the pool sees the assumed-role ARN, not the role ARN granted.
    assert "assumed-role ARN" in message


async def test_invalid_grant_points_at_the_subject_token_itself(role_env):
    auth = gcp_auth(
        sts_payload={"error": "invalid_grant", "error_description": "bad"}, sts_status=400
    )

    with pytest.raises(AdapterError) as exc:
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://gcp.example/a2a")):
            pass

    assert "rejected outright" in str(exc.value)


async def test_an_impersonation_403_says_it_is_not_about_the_pool(role_env):
    """This denial names the service account and reads as a federation failure,
    when the federation already succeeded one call earlier."""
    auth = gcp_auth(iam_payload={"error": {"message": "denied"}}, iam_status=403)

    with pytest.raises(AdapterError) as exc:
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://gcp.example/a2a")):
            pass

    message = str(exc.value)
    assert "roles/iam.serviceAccountTokenCreator" in message
    assert "already succeeded" in message


async def test_the_google_sts_body_is_logged_whole_at_the_boundary(role_env, caplog):
    """A raised message is not an observable -- it can be paraphrased by a model
    in the middle. The body has to be in the log too."""
    auth = gcp_auth(
        sts_payload={"error": "invalid_request", "error_description": "the header list disagrees"},
        sts_status=400,
    )

    with caplog.at_level(logging.ERROR, logger="coordinator.aws_origin"), pytest.raises(AdapterError):
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://gcp.example/a2a")):
            pass

    assert "the header list disagrees" in caplog.text


async def test_the_gcp_id_token_is_cached_across_calls(role_env):
    seen: list[httpx.Request] = []
    auth = gcp_auth(record=seen)

    for _ in range(3):
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://gcp.example/a2a")):
            pass

    assert len([r for r in seen if "sts.googleapis.com" in str(r.url)]) == 1


# --------------------------------------------------------------------------
# AWS -> Azure, the leg that is not keyless
# --------------------------------------------------------------------------


def entra_auth(
    payload: dict,
    status: int = 200,
    record: list | None = None,
    *,
    secret_source: SecretsManagerValue | None = None,
    client_secret: str | None = "literal-secret",
) -> EntraClientSecretAuth:
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        return httpx.Response(status, json=payload)

    return EntraClientSecretAuth(
        tenant_id="tenant-uuid",
        client_id="client-uuid",
        scope="api://currency/.default",
        client_secret=client_secret,
        secret_source=secret_source,
        transport=transport(handler),
    )


def secrets_manager(secret: str, *, record: list | None = None, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        if status != 200:
            return httpx.Response(status, json={"__type": "AccessDeniedException"})
        return httpx.Response(200, json={"SecretString": secret})

    return SecretsManagerValue(
        "arn:aws:secretsmanager:us-west-2:123456789012:secret:currency-mesh/azure-abc",
        region="us-west-2",
        transport=transport(handler),
    )


async def test_the_client_secret_leg_announces_that_it_is_not_keyless(caplog):
    """A silent fallback is exactly how "we deployed a secretless mesh" gets
    written about a mesh with a secret in it."""
    with caplog.at_level(logging.WARNING, logger="coordinator.aws_origin"):
        auth = entra_auth({"access_token": "t", "expires_in": 3600})

    assert auth.mode == "entra-client-secret"
    assert "NOT keyless" in caplog.text
    assert is_keyless("entra-client-secret") is False


async def test_a_leg_with_neither_a_secret_nor_an_arn_is_refused():
    with pytest.raises(AdapterError) as exc:
        entra_auth({}, client_secret=None, secret_source=None)

    assert exc.value.kind is FailureKind.VALIDATION
    assert "AZURE_A2A_CLIENT_SECRET_ARN" in str(exc.value)


async def test_the_secret_is_read_from_secrets_manager_at_first_use(role_env):
    """Lazy on purpose: the two keyless legs must not be blocked on a secret
    they do not use, and a master with no Azure leg must still start."""
    secret_calls: list[httpx.Request] = []
    entra_calls: list[httpx.Request] = []
    auth = entra_auth(
        {"access_token": "entra-token", "expires_in": 3600},
        record=entra_calls,
        client_secret=None,
        secret_source=secrets_manager("from-secrets-manager", record=secret_calls),
    )

    assert secret_calls == []

    async for _ in auth.async_auth_flow(httpx.Request("POST", "https://azure.example/a2a")):
        pass

    assert "client_secret=from-secrets-manager" in entra_calls[0].content.decode()


async def test_the_secrets_manager_call_is_sigv4_signed_with_a_target(role_env):
    seen: list[httpx.Request] = []
    await secrets_manager("s", record=seen).value()

    request = seen[0]
    assert request.headers["x-amz-target"] == "secretsmanager.GetSecretValue"
    assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    # Signed, not merely present.
    assert "x-amz-target" in request.headers["Authorization"]
    assert "/us-west-2/secretsmanager/aws4_request" in request.headers["Authorization"]


async def test_the_secret_is_fetched_once_and_reused(role_env):
    seen: list[httpx.Request] = []
    source = secrets_manager("s", record=seen)

    await source.value()
    await source.value()

    assert len(seen) == 1


async def test_a_json_secret_can_name_its_field(role_env):
    """Both storage shapes are common, and guessing wrong yields an Entra error
    that says only "invalid client secret" -- a rotation problem, apparently."""
    source = secrets_manager(json.dumps({"client_secret": "inner"}))
    source._json_key = "client_secret"

    assert await source.value() == "inner"


async def test_a_missing_json_field_names_the_key_it_wanted(role_env):
    source = secrets_manager(json.dumps({"other": "x"}))
    source._json_key = "client_secret"

    with pytest.raises(AdapterError) as exc:
        await source.value()

    assert "client_secret" in str(exc.value)


async def test_a_denied_secret_read_is_an_auth_failure(role_env):
    with pytest.raises(AdapterError) as exc:
        await secrets_manager("s", status=403).value()

    assert exc.value.kind is FailureKind.AUTHENTICATION


async def test_entra_error_surfaces_the_aadsts_code():
    auth = entra_auth(
        {"error": "invalid_client", "error_description": "AADSTS7000215: invalid client secret"},
        status=401,
    )

    with pytest.raises(AdapterError) as exc:
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://azure.example/a2a")):
            pass

    assert "AADSTS7000215" in str(exc.value)


async def test_entra_access_token_is_cached_until_it_nears_expiry():
    seen: list[httpx.Request] = []
    auth = entra_auth({"access_token": "entra-token", "expires_in": 3600}, record=seen)

    for _ in range(3):
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://azure.example/a2a")):
            pass

    assert len(seen) == 1


async def test_short_lived_entra_token_is_re_exchanged():
    """expires_in is parsed; nothing until now asserted that it is obeyed."""
    seen: list[httpx.Request] = []
    auth = entra_auth({"access_token": "entra-token", "expires_in": 30}, record=seen)

    for _ in range(3):
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://azure.example/a2a")):
            pass

    assert len(seen) == 3


async def test_entra_response_without_expires_in_is_treated_as_an_hour():
    """The documented default. If it were read as 0 every call would re-exchange."""
    seen: list[httpx.Request] = []
    auth = entra_auth({"access_token": "entra-token"}, record=seen)

    for _ in range(2):
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://azure.example/a2a")):
            pass

    assert len(seen) == 1


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


def test_peers_are_unauthenticated_by_default(monkeypatch):
    """The local mesh must stay an unauthenticated protocol instrument."""
    monkeypatch.delenv("GCP_A2A_AUTH", raising=False)

    assert credentials_for("gcp", "http://127.0.0.1:10001") is None
    assert auth_mode(None) == "none"


def test_unknown_mode_is_rejected_rather_than_silently_unauthenticated(monkeypatch):
    monkeypatch.setenv("AWS_A2A_AUTH", "sigv4")

    with pytest.raises(AdapterError) as exc:
        credentials_for("aws", "https://agentcore.example")

    assert exc.value.kind is FailureKind.VALIDATION


def test_the_retired_gcp_rooted_modes_are_gone(monkeypatch):
    """These worked while the master ran on Cloud Run. Leaving them nameable
    would let a stale wiring look configured and then fail at the mint."""
    for mode in ("google-id-token", "aws-sigv4", "entra-fic"):
        monkeypatch.setenv("GCP_A2A_AUTH", mode)
        with pytest.raises(AdapterError):
            credentials_for("gcp", "https://x.example")


def test_missing_required_setting_names_the_variable(monkeypatch):
    monkeypatch.setenv("GCP_A2A_AUTH", "gcp-wif-aws")
    monkeypatch.delenv("GCP_A2A_POOL_PROVIDER", raising=False)

    with pytest.raises(AdapterError) as exc:
        credentials_for("gcp", "https://currency-gcp.a.run.app")

    assert "GCP_A2A_POOL_PROVIDER" in str(exc.value)


def test_the_signing_region_falls_back_to_the_runtimes_own(monkeypatch):
    """Every AWS runtime sets AWS_REGION, so requiring the per-peer variable
    would be asking the operator to restate a fact -- and a restated fact is
    one that can disagree."""
    monkeypatch.setenv("AWS_A2A_AUTH", "aws-sigv4-role")
    monkeypatch.delenv("AWS_A2A_REGION", raising=False)
    monkeypatch.setenv("AWS_REGION", "eu-west-1")

    auth = credentials_for("aws", "https://agentcore.example")

    assert auth._region == "eu-west-1"


def test_no_region_anywhere_names_both_variables(monkeypatch):
    monkeypatch.setenv("AWS_A2A_AUTH", "aws-sigv4-role")
    monkeypatch.delenv("AWS_A2A_REGION", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)

    with pytest.raises(AdapterError) as exc:
        credentials_for("aws", "https://agentcore.example")

    assert "AWS_A2A_REGION" in str(exc.value) and "AWS_REGION" in str(exc.value)


def test_gcp_audience_defaults_to_the_service_root(monkeypatch):
    monkeypatch.setenv("GCP_A2A_AUTH", "gcp-wif-aws")
    monkeypatch.setenv("GCP_A2A_POOL_PROVIDER", "//iam.googleapis.com/projects/1/x")
    monkeypatch.setenv("GCP_A2A_SERVICE_ACCOUNT", "sa@example.iam.gserviceaccount.com")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.delenv("GCP_A2A_AUDIENCE", raising=False)

    auth = credentials_for("gcp", "https://currency-gcp-abc.a.run.app/some/path")

    assert auth.mode == "gcp-wif-aws"
    assert auth._audience == "https://currency-gcp-abc.a.run.app"


def test_entra_registry_builds_a_configured_credential(monkeypatch):
    monkeypatch.setenv("AZURE_A2A_AUTH", "entra-client-secret")
    monkeypatch.setenv("AZURE_A2A_TENANT_ID", "tenant-uuid")
    monkeypatch.setenv("AZURE_A2A_CLIENT_ID", "client-uuid")
    monkeypatch.setenv("AZURE_A2A_CLIENT_SECRET", "s")
    monkeypatch.delenv("AZURE_A2A_SCOPE", raising=False)
    monkeypatch.delenv("AZURE_A2A_CLIENT_SECRET_ARN", raising=False)

    auth = credentials_for("azure", "https://azure.example")

    assert auth.mode == "entra-client-secret"
    assert auth._scope == "client-uuid/.default"


def test_exactly_one_mode_needs_a_stored_secret():
    """The claim this project has to back, asserted rather than described."""
    assert [mode for mode in ("aws-sigv4-role", "gcp-wif-aws", "entra-client-secret")
            if not is_keyless(mode)] == ["entra-client-secret"]


def test_the_card_fetch_carries_the_same_credential(role_env):
    """Discovery is privileged on all three clouds. A card fetch that 403s while
    the call would have succeeded surfaces as a protocol error, nowhere near
    auth."""
    from clients.base import A2AQuoteClient

    auth = AwsRoleSigV4Auth(region="us-west-2")
    client = A2AQuoteClient("https://agentcore.example", auth=auth)

    async def check():
        async with client._http_client() as httpx_client:
            assert httpx_client.auth is auth

    import asyncio

    asyncio.run(check())


# --------------------------------------------------------------------------
# Token lifecycle: expiry, refresh and skew
#
# No deployed run has ever aged a token: the master answers in seconds. These
# branches are reachable here because expiry is a clock question, not a network
# one.
# --------------------------------------------------------------------------


def test_the_skew_window_is_what_makes_a_live_token_unusable():
    """Inside the skew a token is still valid but must not be handed out.

    This is the whole reason the seam refreshes early: a token that expires
    mid-flight fails at the provider, where the error is someone else's.
    """
    inside = _CachedToken("t", datetime.now(UTC) + _EXPIRY_SKEW - timedelta(seconds=5))
    outside = _CachedToken("t", datetime.now(UTC) + _EXPIRY_SKEW + timedelta(seconds=5))

    assert inside.usable is False
    assert outside.usable is True


def test_an_already_expired_credential_is_not_usable():
    """Clock skew the wrong way: the provider's expiry is already behind us."""
    past = _AwsCredentials("k", "s", "t", datetime.now(UTC) - timedelta(minutes=10))
    assert past.usable is False


def test_aws_and_google_agree_on_the_skew():
    """Two usable properties, one policy -- they must not drift apart."""
    expires_at = datetime.now(UTC) + _EXPIRY_SKEW - timedelta(seconds=1)
    assert _CachedToken("t", expires_at).usable is False
    assert _AwsCredentials("k", "s", "t", expires_at).usable is False


async def test_expiring_container_credentials_are_re_resolved(monkeypatch):
    """Without this the refresh branch is never executed anywhere: a long-lived
    expiry exercises only the cache hit."""
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", "http://169.254.170.2/creds")
    seen: list[httpx.Request] = []
    soon = (datetime.now(UTC) + timedelta(seconds=30)).isoformat().replace("+00:00", "Z")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "AccessKeyId": "k",
                "SecretAccessKey": "s",
                "Token": "t",
                "Expiration": soon,
            },
        )

    credentials = AwsWorkloadCredentials(transport=transport(handler))
    for _ in range(3):
        await credentials.credentials()

    assert len(seen) == 3


def test_a_naive_expiry_does_not_defer_a_typeerror_into_the_next_call():
    """AWS sends a Z suffix. If it ever did not, the crash landed in `usable`.

    `fromisoformat` accepts a value with no offset, so parsing succeeded and
    the naive/aware comparison blew up one call later, at a line with nothing
    to do with the cause. Both providers document UTC, so assume it.
    """
    parsed = _parse_expiry("2099-01-01T00:00:00", "test")

    assert parsed.tzinfo is not None
    assert _AwsCredentials("k", "s", "t", parsed).usable is True


def test_an_unparseable_expiry_is_a_named_auth_failure_not_a_valueerror():
    """ValueError is not an AdapterError, so it travelled back unmapped."""
    with pytest.raises(AdapterError) as exc:
        _parse_expiry("whenever", "test")

    assert "whenever" in str(exc.value)


def test_a_misconfigured_peer_fails_its_cell_not_the_matrix(monkeypatch):
    """One unconfigured cloud must not stop the other six cells running."""
    import asyncio
    from decimal import Decimal

    from coordinator.models import ConversionRequest
    from matrix.runner import Server, probe

    monkeypatch.setenv("GCP_A2A_AUTH", "gcp-wif-aws")
    monkeypatch.delenv("GCP_A2A_POOL_PROVIDER", raising=False)

    cell = asyncio.run(
        probe(
            "a2a-sdk",
            Server("gcp", "Google Cloud", "adk to_a2a", "https://currency-gcp.a.run.app"),
            ConversionRequest(
                amount=Decimal(100), source_currency="USD", target_currencies=["EUR"]
            ),
            timeout_s=1.0,
        )
    )

    assert cell.ok is False
    assert cell.failure_kind == FailureKind.VALIDATION.value
