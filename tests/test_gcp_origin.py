"""Cloud-free tests of the GCP-rooted credential seam.

The mirror of ``test_auth.py``, for the master running as a Cloud Run service.
Every provider is a stubbed transport, so what this asserts is the shape of each
exchange and which layer a denial names -- not that Google, AWS or Entra accept
any of it.

The one claim worth stating plainly: all three legs here are keyless, and the
Azure leg is the reason the comparison exists. From Cloud Run it federates; from
AgentCore the same leg carries a client secret.
"""

import base64
import json
import time
from urllib.parse import parse_qs

import httpx
import pytest

from coordinator.errors import AdapterError
from coordinator.gcp_origin import (
    AwsWebIdentitySigV4Auth,
    EntraFederatedAuth,
    GoogleIdTokenAuth,
    GoogleWorkloadIdentity,
    _entra_detail,
    _parse_sts_credentials,
    _sts_detail,
)


def transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def jwt(exp_offset_s: int = 3600, **claims) -> str:
    """A structurally valid unsigned JWT. Nothing here verifies a signature."""
    payload = {"exp": int(time.time()) + exp_offset_s, **claims}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{encoded}.signature"


STS_OK = """<AssumeRoleWithWebIdentityResponse
  xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <AssumeRoleWithWebIdentityResult>
    <Credentials>
      <AccessKeyId>ASIAFEDERATED</AccessKeyId>
      <SecretAccessKey>federated-secret</SecretAccessKey>
      <SessionToken>federated-session</SessionToken>
      <Expiration>2099-01-01T00:00:00Z</Expiration>
    </Credentials>
  </AssumeRoleWithWebIdentityResult>
</AssumeRoleWithWebIdentityResponse>"""


def sts_error(code: str, message: str = "denied") -> str:
    return (
        '<ErrorResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
        f"<Error><Code>{code}</Code><Message>{message}</Message></Error>"
        "</ErrorResponse>"
    )


# --------------------------------------------------------------------------
# The metadata mint, which all three legs share
# --------------------------------------------------------------------------


async def test_the_mint_asks_for_format_full():
    """Without format=full Google trims the token and drops the email claim,
    which the AWS trust policy and the Entra FIC both depend on."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=jwt())

    identity = GoogleWorkloadIdentity(transport=transport(handler))
    await identity.id_token("https://currency-gcp.a.run.app")

    assert parse_qs(seen[0].url.query.decode())["format"] == ["full"]
    assert seen[0].headers["Metadata-Flavor"] == "Google"


async def test_tokens_are_cached_per_audience():
    """A token minted for Azure is refused by AWS, so one cache slot per
    audience is the difference between working and a confusing 403."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        audience = parse_qs(request.url.query.decode())["audience"][0]
        seen.append(audience)
        return httpx.Response(200, text=jwt(audience=audience))

    identity = GoogleWorkloadIdentity(transport=transport(handler))
    await identity.id_token("sts.amazonaws.com")
    await identity.id_token("sts.amazonaws.com")
    await identity.id_token("api://AzureADTokenExchange")

    assert seen == ["sts.amazonaws.com", "api://AzureADTokenExchange"]


async def test_an_expired_cached_token_is_reminted():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, text=jwt(exp_offset_s=-10))

    identity = GoogleWorkloadIdentity(transport=transport(handler))
    await identity.id_token("aud")
    await identity.id_token("aud")

    assert calls == 2


async def test_an_unreachable_metadata_server_says_the_mode_is_gcp_rooted():
    """The failure a stale wiring produces. Off GCP there is no metadata server,
    and 'connection refused' alone reads as an outage rather than as a leg
    configured for the wrong host."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    identity = GoogleWorkloadIdentity(transport=transport(handler))

    with pytest.raises(AdapterError) as exc:
        await identity.id_token("aud")

    assert "GCP-rooted" in str(exc.value)


async def test_a_token_with_no_exp_is_an_auth_error_not_a_value_error():
    """An unparseable expiry must arrive as a named auth failure. A ValueError
    travels back unmapped -- the same trap _parse_expiry exists to close."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not.a.jwt")

    identity = GoogleWorkloadIdentity(transport=transport(handler))

    with pytest.raises(AdapterError):
        await identity.id_token("aud")


# --------------------------------------------------------------------------
# GCP -> GCP
# --------------------------------------------------------------------------


async def test_the_gcp_leg_presents_the_minted_id_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=jwt())

    auth = GoogleIdTokenAuth(
        audience="https://currency-gcp.a.run.app", transport=transport(handler)
    )

    async for signed in auth.async_auth_flow(httpx.Request("POST", "https://gcp.example/a2a")):
        assert signed.headers["Authorization"].startswith("Bearer ")

    assert auth.mode == "google-id-token"


async def test_the_gcp_audience_is_the_service_root_not_the_endpoint():
    """Cloud Run validates aud against its own service URL, so a token minted
    for the full path is refused."""
    from coordinator.auth import _service_root

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(parse_qs(request.url.query.decode())["audience"][0])
        return httpx.Response(200, text=jwt())

    leg = GoogleIdTokenAuth(
        audience=_service_root("https://currency-gcp.a.run.app/a2a/messages"),
        transport=transport(handler),
    )
    async for _ in leg.async_auth_flow(httpx.Request("POST", "https://gcp.example/a2a")):
        pass

    assert seen == ["https://currency-gcp.a.run.app"]


# --------------------------------------------------------------------------
# GCP -> AWS
# --------------------------------------------------------------------------


def aws_leg(sts_body: str = STS_OK, sts_status: int = 200, record: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        if "metadata.google.internal" in str(request.url):
            return httpx.Response(200, text=jwt())
        if "sts." in str(request.url) and "amazonaws" in str(request.url):
            return httpx.Response(sts_status, text=sts_body)
        raise AssertionError(f"unexpected host: {request.url}")

    return AwsWebIdentitySigV4Auth(
        role_arn="arn:aws:iam::123456789012:role/currency-aws-federated",
        region="us-west-2",
        service="bedrock-agentcore",
        transport=transport(handler),
    )


async def test_the_aws_leg_trades_the_token_for_role_credentials_then_signs():
    seen: list[httpx.Request] = []
    auth = aws_leg(record=seen)

    async for signed in auth.async_auth_flow(
        httpx.Request("POST", "https://bedrock-agentcore.us-west-2.amazonaws.com/x")
    ):
        assert signed.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert "ASIAFEDERATED" in signed.headers["Authorization"]

    sts_call = next(r for r in seen if r.url.host.startswith("sts."))
    form = parse_qs(sts_call.content.decode())
    assert form["Action"] == ["AssumeRoleWithWebIdentity"]
    assert form["RoleArn"] == ["arn:aws:iam::123456789012:role/currency-aws-federated"]


async def test_the_web_identity_token_is_minted_for_the_sts_audience():
    """AWS reads this as accounts.google.com:oaud. Getting it wrong denies with
    AccessDenied and names nothing."""
    seen: list[httpx.Request] = []
    auth = aws_leg(record=seen)

    async for _ in auth.async_auth_flow(httpx.Request("POST", "https://agentcore.example/x")):
        pass

    mint = next(r for r in seen if r.url.host == "metadata.google.internal")
    assert parse_qs(mint.url.query.decode())["audience"] == ["sts.amazonaws.com"]


async def test_the_sts_call_is_unsigned():
    """The web identity token is the credential; signing it would need the
    credentials this call exists to obtain."""
    seen: list[httpx.Request] = []
    auth = aws_leg(record=seen)

    async for _ in auth.async_auth_flow(httpx.Request("POST", "https://agentcore.example/x")):
        pass

    sts_call = next(r for r in seen if r.url.host.startswith("sts."))
    assert "Authorization" not in sts_call.headers


async def test_role_credentials_are_cached_across_calls():
    seen: list[httpx.Request] = []
    auth = aws_leg(record=seen)

    for _ in range(2):
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://agentcore.example/x")):
            pass

    assert len([r for r in seen if r.url.host.startswith("sts.")]) == 1


async def test_invalid_identity_token_points_at_the_provider_not_the_policy():
    """The distinction that separates a provider-setup bug from a condition bug,
    and the one an explicit accounts.google.com OIDC provider triggers."""
    auth = aws_leg(sts_body=sts_error("InvalidIdentityToken"), sts_status=403)

    with pytest.raises(AdapterError) as exc:
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://agentcore.example/x")):
            pass

    assert "InvalidIdentityToken" in str(exc.value)
    assert "explicit IAM OIDC provider" in str(exc.value)


async def test_access_denied_points_at_the_trust_conditions():
    auth = aws_leg(sts_body=sts_error("AccessDenied"), sts_status=403)

    with pytest.raises(AdapterError) as exc:
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://agentcore.example/x")):
            pass

    assert "accounts.google.com:sub" in str(exc.value)


def test_a_credentials_element_missing_a_field_is_named():
    body = STS_OK.replace("<SessionToken>federated-session</SessionToken>", "")

    with pytest.raises(AdapterError) as exc:
        _parse_sts_credentials(body, "boundary")

    assert "SessionToken" in str(exc.value)


def test_unparseable_sts_xml_does_not_escape_as_a_parse_error():
    with pytest.raises(AdapterError):
        _parse_sts_credentials("<not-xml", "boundary")


def test_sts_detail_falls_back_to_the_body_when_xml_is_not_xml():
    response = httpx.Response(500, text="upstream exploded")
    assert "upstream exploded" in _sts_detail(response)


# --------------------------------------------------------------------------
# GCP -> Azure
# --------------------------------------------------------------------------


def azure_leg(payload: dict | None = None, status: int = 200, record: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        if "metadata.google.internal" in str(request.url):
            return httpx.Response(200, text=jwt())
        if "login.microsoftonline.com" in str(request.url):
            return httpx.Response(
                status, json=payload or {"access_token": "entra-access", "expires_in": 3600}
            )
        raise AssertionError(f"unexpected host: {request.url}")

    return EntraFederatedAuth(
        tenant_id="tenant-id",
        client_id="client-id",
        transport=transport(handler),
    )


async def test_the_azure_leg_is_keyless_and_sends_a_client_assertion():
    """The whole difference from the AgentCore topology: a Google-minted
    assertion stands in for a client secret, so nothing long-lived exists."""
    seen: list[httpx.Request] = []
    auth = azure_leg(record=seen)

    async for signed in auth.async_auth_flow(httpx.Request("POST", "https://azure.example/a2a")):
        assert signed.headers["Authorization"] == "Bearer entra-access"

    form = parse_qs(next(r for r in seen if r.url.host == "login.microsoftonline.com").content.decode())
    assert form["client_assertion_type"] == [
        "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    ]
    assert "client_secret" not in form
    assert auth.mode == "entra-fic"


async def test_the_assertion_is_minted_for_the_entra_exchange_audience():
    seen: list[httpx.Request] = []
    auth = azure_leg(record=seen)

    async for _ in auth.async_auth_flow(httpx.Request("POST", "https://azure.example/a2a")):
        pass

    mint = next(r for r in seen if r.url.host == "metadata.google.internal")
    assert parse_qs(mint.url.query.decode())["audience"] == ["api://AzureADTokenExchange"]


async def test_the_entra_token_is_cached_for_its_expires_in():
    seen: list[httpx.Request] = []
    auth = azure_leg(record=seen)

    for _ in range(2):
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://azure.example/a2a")):
            pass

    assert len([r for r in seen if r.url.host == "login.microsoftonline.com"]) == 1


async def test_a_short_expires_in_is_honoured():
    """A token that expires inside the skew window must not be reused."""
    seen: list[httpx.Request] = []
    auth = azure_leg(payload={"access_token": "brief", "expires_in": 1}, record=seen)

    for _ in range(2):
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://azure.example/a2a")):
            pass

    assert len([r for r in seen if r.url.host == "login.microsoftonline.com"]) == 2


async def test_no_matching_federated_credential_is_named():
    auth = azure_leg(
        payload={"error": "invalid_client", "error_description": "AADSTS70021: No matching FIC"},
        status=401,
    )

    with pytest.raises(AdapterError) as exc:
        async for _ in auth.async_auth_flow(httpx.Request("POST", "https://azure.example/a2a")):
            pass

    assert "issuer and subject" in str(exc.value)


def test_entra_detail_flags_a_wrong_assertion_audience():
    response = httpx.Response(
        401, json={"error": "invalid_request", "error_description": "AADSTS700212: bad aud"}
    )
    assert "api://AzureADTokenExchange" in _entra_detail(response)


def test_entra_detail_falls_back_when_the_body_is_not_json():
    response = httpx.Response(500, text="gateway error")
    assert "gateway error" in _entra_detail(response)
