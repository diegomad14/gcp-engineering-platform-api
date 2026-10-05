"""OAuth opt-in and browser consent; no provider credentials or messages are used."""

import base64
import hashlib
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull

from eng_platform_api.config import config
from eng_platform_api.main import app
from eng_platform_api.services import mcp_auth, mcp_store

BASE = "http://localhost:8000"
READ = "eng-platform.access"
LEGACY = "eng-platform.read"
VERIFIER = "v" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest())
    .rstrip(b"=")
    .decode()
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for records in mcp_store._memory.values():
        records.clear()
    monkeypatch.setattr(config, "mock_mode", True)
    monkeypatch.setattr(config.mcp, "enabled", True)
    monkeypatch.setattr(config.mcp, "public_base_url", BASE)
    monkeypatch.setattr(config.mcp, "issuer_url", BASE)
    monkeypatch.setattr(config.auth, "allowed_logins", ("diegomad14", "reader"))
    monkeypatch.setattr(config.auth, "github_client_id", "unit-client")
    monkeypatch.setattr(config.auth, "github_client_secret", "unit-secret")
    actor = {"login": "diegomad14"}

    class GitHub:
        def __init__(self, **kwargs):
            pass

        def create_authorization_url(self, url, **kwargs):
            assert kwargs["scope"] == "read:user"
            return url + "?state=" + kwargs["state"], "unused"

        async def fetch_token(self, *args, **kwargs):
            return {"access_token": "fake-upstream"}

        async def get(self, *args, **kwargs):
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: actor)

    monkeypatch.setattr(mcp_auth, "AsyncOAuth2Client", GitHub)
    return actor


async def begin(client, scopes):
    registration = OAuthClientInformationFull(
        client_id="cost-client",
        client_name='<Client "name">',
        redirect_uris=["https://client.example/callback"],
        token_endpoint_auth_method="none",
        scope=" ".join(scopes),
    )
    await mcp_auth.provider.register_client(registration)
    location = await mcp_auth.provider.authorize(
        registration,
        AuthorizationParams(
            scopes=scopes,
            redirect_uri=registration.redirect_uris[0],
            redirect_uri_provided_explicitly=True,
            code_challenge=CHALLENGE,
            state="client-state",
            resource=BASE + "/mcp/cost-alerts",
        ),
    )
    state = parse_qs(urlparse(location).query)["state"][0]
    return client.get(
        "/api/auth/callback",
        params={"state": state, "code": "fake-github"},
        follow_redirects=False,
    )


def decision(client, callback, **changes):
    consent = parse_qs(urlparse(callback.headers["location"]).query)["consent"][0]
    data = {
        "consent": consent,
        "csrf": client.cookies.get("eng_platform_mcp_consent"),
        "decision": "allow",
        **changes,
    }
    return client.post(
        "/mcp/consent", data=data, headers={"Origin": BASE}, follow_redirects=False
    )


def protocol(response):
    assert response.status_code == 200, response.text
    if response.headers.get("content-type", "").startswith("application/json"):
        return response.json()
    return json.loads(
        next(
            line[6:] for line in response.text.splitlines() if line.startswith("data: ")
        )
    )


def test_both_endpoints_discover_uniform_full_access():
    with TestClient(app, base_url=BASE) as client:
        ordinary = client.get("/.well-known/oauth-protected-resource/mcp").json()
        alert = client.get(
            "/.well-known/oauth-protected-resource/mcp/cost-alerts"
        ).json()
        assert ordinary["scopes_supported"] == [READ]
        assert alert["scopes_supported"] == [READ]
        assert alert["resource"] == BASE + "/mcp/cost-alerts"
        assert alert["authorization_servers"] == [BASE + "/"]
        unauthorized = client.get("/mcp/cost-alerts")
        assert unauthorized.status_code == 401
        header = unauthorized.headers["www-authenticate"]
        assert 'scope="eng-platform.access"' in header
        assert (
            f'resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp/cost-alerts"'
            in header
        )
        assert "deploy" not in header and "rollback" not in header
        assert "eng-platform.deploy" not in ordinary["scopes_supported"]
        registered = client.post(
            "/register",
            json={
                "redirect_uris": ["https://client.example/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        assert registered.status_code == 201
        assert registered.json()["scope"] == READ


@pytest.mark.asyncio
async def test_any_github_login_requires_explicit_full_consent(isolated):
    isolated["login"] = "outside-allowlists"
    with TestClient(app, base_url=BASE) as client:
        callback = await begin(client, [READ])
        assert (
            callback.status_code == 302
            and "/mcp/consent?" in callback.headers["location"]
        )
        assert not mcp_store._memory["code"]
        assert decision(client, callback).status_code == 303
    record = next(iter(mcp_store._memory["code"].values()))
    assert record["scopes"] == [READ] and record["subject"] == "outside-allowlists"


@pytest.mark.asyncio
async def test_explicit_send_requires_consent_then_pkce_and_does_not_elevate_old_token():
    mcp_store.save(
        "access",
        mcp_store.token_key("old-reader"),
        {
            "client_id": "old-reader",
            "scopes": [LEGACY],
            "subject": "diegomad14",
            "expires_at": 9999999999,
        },
    )
    with TestClient(app, base_url=BASE) as client:
        callback = await begin(client, [READ])
        assert (
            callback.status_code == 302
            and "/mcp/consent?" in callback.headers["location"]
        )
        assert "HttpOnly" in callback.headers["set-cookie"]
        assert not mcp_store._memory["code"]
        page = client.get(callback.headers["location"])
        assert page.status_code == 200
        assert "desplegar y revertir" in page.text and "&lt;Client" in page.text
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert not mcp_store._memory["code"]
        result = decision(client, callback)
        assert result.status_code == 303
        query = parse_qs(urlparse(result.headers["location"]).query)
        assert query["state"] == ["client-state"]
        code = query["code"][0]
        body = {
            "grant_type": "authorization_code",
            "client_id": "cost-client",
            "code": code,
            "redirect_uri": "https://client.example/callback",
            "code_verifier": VERIFIER,
        }
        assert (
            client.post("/token", data={**body, "code_verifier": "wrong"}).status_code
            == 400
        )
        token = client.post("/token", data=body)
        assert token.status_code == 200
        assert set(token.json()["scope"].split()) == {READ}
        assert client.post("/token", data=body).status_code == 400
        assert decision(client, callback).status_code == 403
    assert await mcp_auth.provider.verify_token("old-reader") is None


@pytest.mark.asyncio
async def test_cancel_bad_csrf_cross_origin_and_expired_consent_issue_no_codes(
    monkeypatch,
):
    with TestClient(app, base_url=BASE) as client:
        callback = await begin(client, [READ])
        assert decision(client, callback, csrf="wrong").status_code == 403
        consent = parse_qs(urlparse(callback.headers["location"]).query)["consent"][0]
        data = {
            "consent": consent,
            "csrf": client.cookies.get("eng_platform_mcp_consent"),
            "decision": "allow",
        }
        assert (
            client.post(
                "/mcp/consent", data=data, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )
        assert decision(client, callback, decision="unknown").status_code == 400
        cancelled = decision(client, callback, decision="deny")
        assert (
            cancelled.status_code == 303
            and "error=access_denied" in cancelled.headers["location"]
        )
        assert not mcp_store._memory["code"]
        callback = await begin(client, [READ])
        record = next(iter(mcp_store._memory["consent"].values()))
        record["expires_at"] = 0
        assert client.get(callback.headers["location"]).status_code == 403
        assert decision(client, callback).status_code == 403
        assert not mcp_store._memory["code"]
        monkeypatch.setattr(config.mcp, "enabled", False)
        assert (
            client.get(
                "/.well-known/oauth-protected-resource/mcp/cost-alerts"
            ).status_code
            == 404
        )
        assert client.get("/mcp/consent").status_code == 404
        assert decision(client, callback).status_code == 404


@pytest.mark.asyncio
async def test_other_account_can_consent_to_all_actions(isolated):
    isolated["login"] = "reader"
    with TestClient(app, base_url=BASE) as client:
        callback = await begin(client, [READ])
        assert callback.status_code == 302
        assert decision(client, callback).status_code == 303
    assert next(iter(mcp_store._memory["code"].values()))["subject"] == "reader"


@pytest.mark.asyncio
async def test_transport_describes_send_scope_and_returns_chatgpt_scope_challenge(
    monkeypatch,
):
    from eng_platform_api.services import cost_alerts

    monkeypatch.setattr(
        cost_alerts, "send", lambda subject, **kwargs: {"status": "test_no_send"}
    )
    issued = await mcp_auth.provider._issue_tokens(
        client_id="reader", scopes=[READ], subject="diegomad14", resource=BASE + "/mcp"
    )
    headers = {
        "Authorization": "Bearer " + issued.access_token,
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
    }
    with TestClient(app, base_url=BASE) as client:
        listing = protocol(
            client.post(
                "/mcp/cost-alerts",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
        )
        tool = next(
            t for t in listing["result"]["tools"] if t["name"] == "send_cost_alert"
        )
        assert tool["securitySchemes"] == [{"type": "oauth2", "scopes": [READ]}]
        assert tool["_meta"]["securitySchemes"] == tool["securitySchemes"]
        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "send_cost_alert", "arguments": {}},
        }
        legacy_headers = {**headers, "Authorization": "Bearer old-reader"}
        assert (
            client.post(
                "/mcp/cost-alerts", headers=legacy_headers, json=call
            ).status_code
            == 401
        )
        explicit = await mcp_auth.provider._issue_tokens(
            client_id="alerts",
            scopes=[READ],
            subject="diegomad14",
            resource=BASE + "/mcp/cost-alerts",
        )
        success = protocol(
            client.post(
                "/mcp/cost-alerts",
                headers={**headers, "Authorization": "Bearer " + explicit.access_token},
                json=call,
            )
        )["result"]
        assert not success["isError"] and success["structuredContent"] == {
            "status": "test_no_send"
        }


@pytest.mark.asyncio
async def test_legacy_registration_and_refresh_require_reconnection():
    with TestClient(app, base_url=BASE) as client:
        rejected = client.post(
            "/register",
            json={
                "redirect_uris": ["https://client.example/callback"],
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "scope": LEGACY,
            },
        )
        assert rejected.status_code == 400
        mcp_store.save(
            "client",
            "old-client",
            {
                "metadata": OAuthClientInformationFull(
                    client_id="old-client",
                    redirect_uris=["https://client.example/callback"],
                    token_endpoint_auth_method="none",
                    scope=LEGACY,
                ).model_dump(mode="json")
            },
        )
        mcp_store.save(
            "refresh",
            mcp_store.token_key("old-refresh"),
            {"client_id": "old-client", "scopes": [LEGACY], "expires_at": 9999999999},
        )
        assert (
            client.post(
                "/token",
                data={
                    "grant_type": "refresh_token",
                    "client_id": "old-client",
                    "refresh_token": "old-refresh",
                    "scope": READ,
                },
            ).status_code
            == 400
        )
