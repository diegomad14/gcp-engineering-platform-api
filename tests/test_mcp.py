"""Security and contract tests for the remote MCP surface.

These never dispatch an actual workflow or Cloud Build job.
"""

import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.shared.auth import OAuthClientInformationFull

from eng_platform_api.config import config
from eng_platform_api.main import app
from eng_platform_api import mcp_server
from eng_platform_api.mcp_server import list_services, start_deployment
from eng_platform_api.models import DeploymentItem, ReleaseTag, ReleaseTagPage
from eng_platform_api.services import mcp_store
from eng_platform_api.services.mcp_auth import provider


@pytest.fixture(autouse=True)
def isolated_mcp(monkeypatch):
    for values in mcp_store._memory.values():
        values.clear()
    monkeypatch.setattr(config, "mock_mode", True)
    monkeypatch.setattr(config.mcp, "enabled", False)
    monkeypatch.setattr(config.mcp, "public_base_url", "http://testserver")
    monkeypatch.setattr(config.auth, "allowed_logins", ("diegomad14",))


def _client(client_id="mcp-test"):
    return OAuthClientInformationFull(
        client_id=client_id,
        redirect_uris=["http://localhost:3333/callback"],
        token_endpoint_auth_method="none",
        scope="eng-platform.read eng-platform.deploy eng-platform.rollback",
    )


def test_mcp_environment_configuration_requires_a_public_url(monkeypatch):
    from eng_platform_api.config import load_config

    monkeypatch.setenv("ENG_PLATFORM_MCP_ENABLED", "true")
    monkeypatch.setenv("ENG_PLATFORM_MCP_PUBLIC_BASE_URL", "https://api.example.test/")
    monkeypatch.setenv("ENG_PLATFORM_MCP_ISSUER_URL", "https://issuer.example.test/")
    monkeypatch.setenv("ENG_PLATFORM_MCP_AUDIT_FIRESTORE_COLLECTION", "mcp_audit")
    monkeypatch.setenv("ENG_PLATFORM_MCP_OAUTH_FIRESTORE_COLLECTION", "mcp_oauth")
    monkeypatch.setenv("ENG_PLATFORM_MCP_ACCESS_TOKEN_TTL_SECONDS", "120")
    monkeypatch.setenv("ENG_PLATFORM_MCP_REFRESH_TOKEN_TTL_SECONDS", "360")
    monkeypatch.setenv("ENG_PLATFORM_MCP_MUTATION_LIMIT_PER_HOUR", "4")
    loaded = load_config().mcp
    assert loaded.enabled and loaded.public_base_url == "https://api.example.test"
    assert loaded.issuer_url == "https://issuer.example.test"
    assert (loaded.audit_collection, loaded.oauth_collection) == (
        "mcp_audit",
        "mcp_oauth",
    )
    assert (
        loaded.access_token_ttl_seconds,
        loaded.refresh_token_ttl_seconds,
        loaded.mutation_limit_per_hour,
    ) == (120, 360, 4)
    monkeypatch.delenv("ENG_PLATFORM_MCP_PUBLIC_BASE_URL")
    with pytest.raises(ValueError, match="MCP_PUBLIC_BASE_URL"):
        load_config()


@pytest.mark.asyncio
async def test_dcr_uses_public_pkce_client_and_hashed_opaque_tokens():
    client = _client()
    await provider.register_client(client)
    assert (await provider.get_client("mcp-test")).client_id == "mcp-test"
    raw_access = "not-stored-plaintext"
    mcp_store.save(
        "access",
        mcp_store.token_key(raw_access),
        {
            "client_id": "mcp-test",
            "scopes": ["eng-platform.read"],
            "subject": "diegomad14",
            "expires_at": time.time() + 60,
        },
    )
    assert await provider.verify_token(raw_access)
    assert raw_access not in mcp_store._memory["access"]


@pytest.mark.asyncio
async def test_dcr_rejects_non_local_http_and_client_secret_clients():
    unsafe = OAuthClientInformationFull(
        client_id="unsafe",
        redirect_uris=["http://example.test/callback"],
        token_endpoint_auth_method="none",
    )
    with pytest.raises(Exception):
        await provider.register_client(unsafe)
    secret_client = OAuthClientInformationFull(
        client_id="secret",
        redirect_uris=["https://example.test/callback"],
        token_endpoint_auth_method="client_secret_post",
        client_secret="do-not-store",
    )
    with pytest.raises(Exception):
        await provider.register_client(secret_client)


@pytest.mark.asyncio
async def test_refresh_rotation_revokes_previous_access_token():
    client = _client()
    await provider.register_client(client)
    issued = await provider._issue_tokens(
        client_id="mcp-test",
        scopes=["eng-platform.read"],
        subject="diegomad14",
        resource=None,
    )
    refresh = await provider.load_refresh_token(client, issued.refresh_token)
    assert refresh is not None
    rotated = await provider.exchange_refresh_token(client, refresh, refresh.scopes)
    assert await provider.verify_token(issued.access_token) is None
    assert await provider.load_refresh_token(client, issued.refresh_token) is None
    assert await provider.verify_token(rotated.access_token)


def test_feature_flag_hides_mcp_and_enabled_endpoint_requires_oauth(monkeypatch):
    with TestClient(app, base_url="http://localhost:8000") as client:
        assert client.get("/mcp").status_code == 404
        monkeypatch.setattr(config.mcp, "enabled", True)
        response = client.get("/mcp")
        assert response.status_code == 401
        assert "Bearer" in response.headers["www-authenticate"]
        metadata = client.get("/.well-known/oauth-protected-resource/mcp")
        assert metadata.status_code == 200
        assert metadata.json()["resource"].endswith("/mcp")


def test_existing_github_callback_routes_pending_mcp_state(monkeypatch):
    monkeypatch.setattr(config.mcp, "enabled", True)
    mcp_store.save(
        "state",
        mcp_store.token_key("mcp-state"),
        {"expires_at": time.time() + 60},
    )

    async def complete(**_kwargs):
        return "http://localhost:3333/callback?code=opaque"

    monkeypatch.setattr(provider, "complete_github_authorization", complete)
    with TestClient(app) as client:
        response = client.get(
            "/api/auth/callback?state=mcp-state&code=github-code",
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert response.headers["location"].startswith("http://localhost:3333/callback")


def test_streamable_http_initialize_and_tool_discovery(monkeypatch):
    monkeypatch.setattr(config.mcp, "enabled", True)
    mcp_store.save(
        "access",
        mcp_store.token_key("mcp-access"),
        {
            "client_id": "mcp-client",
            "scopes": ["eng-platform.read"],
            "subject": "diegomad14",
            "expires_at": time.time() + 60,
        },
    )
    headers = {
        "Authorization": "Bearer mcp-access",
        "MCP-Protocol-Version": "2025-06-18",
        "Accept": "application/json, text/event-stream",
    }
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }
    with TestClient(app, base_url="http://localhost:8000") as client:
        response = client.post("/mcp", headers=headers, json=initialize)
        assert response.status_code == 200
        assert '"protocolVersion":"2025-06-18"' in response.text
        discovery = client.post(
            "/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
    assert discovery.status_code == 200
    assert '"name":"start_deployment"' in discovery.text
    assert '"name":"start_rollback"' in discovery.text


def test_read_tool_requires_scope_and_audits_no_raw_inputs():
    token = auth_context_var.set(
        AuthenticatedUser(
            AccessToken(
                token="opaque",
                client_id="client",
                scopes=["eng-platform.read"],
                subject="diegomad14",
            )
        )
    )
    try:
        result = list_services()
    finally:
        auth_context_var.reset(token)
    assert result["total"] >= 1
    record = next(iter(mcp_store._memory["audit"].values()))
    assert record["tool"] == "list_services"
    assert len(record["input_fingerprint"]) == 64


def test_mutation_does_not_accept_executor_or_sha_and_requires_deploy_scope():
    token = auth_context_var.set(
        AuthenticatedUser(
            AccessToken(
                token="opaque",
                client_id="client",
                scopes=["eng-platform.read"],
                subject="diegomad14",
            )
        )
    )
    try:
        with pytest.raises(HTTPException) as exc:
            start_deployment(
                "eng-platform-api", "v1.0.0", "approved", "mcp-idempotency"
            )
    finally:
        auth_context_var.reset(token)
    assert exc.value.status_code == 403
    assert "executor" not in str(start_deployment.__annotations__)
    assert "sha" not in str(start_deployment.__annotations__)


def test_tool_errors_are_audited_and_mutation_rate_limit_is_enforced():
    token = auth_context_var.set(
        AuthenticatedUser(
            AccessToken(
                token="opaque",
                client_id="client",
                scopes=["eng-platform.read", "eng-platform.deploy"],
                subject="diegomad14",
            )
        )
    )
    try:
        with pytest.raises(RuntimeError, match="read failure"):
            mcp_server._read(
                "broken_read",
                {"service_name": "eng-platform-api"},
                lambda: (_ for _ in ()).throw(RuntimeError("read failure")),
            )
        for _ in range(config.mcp.mutation_limit_per_hour):
            mcp_store.save_audit(
                {"subject": "diegomad14", "mutation": True, "tool": "prior"}
            )
        with pytest.raises(HTTPException) as exc:
            mcp_server._mutate(
                "limited",
                {"service_name": "eng-platform-api"},
                "eng-platform.deploy",
                lambda _: None,
            )
    finally:
        auth_context_var.reset(token)
    assert exc.value.status_code == 429
    assert any(
        record["result"] == "error" for record in mcp_store._memory["audit"].values()
    )


@pytest.mark.asyncio
async def test_tools_return_only_public_dtos_and_audit_operations(monkeypatch):
    item = DeploymentItem(
        id="deployment-1",
        service_name="eng-platform-api",
        repository="diegomad14/gcp-engineering-platform-api",
        tag="v1.2.3",
        sha="a" * 40,
    )
    monkeypatch.setattr(
        mcp_server.github_deployments,
        "list_tags",
        lambda *_args, **_kwargs: ReleaseTagPage(
            items=[ReleaseTag(name="v1.2.3", sha="a" * 40)]
        ),
    )
    monkeypatch.setattr(
        mcp_server.github_deployments,
        "get_tag",
        lambda *_args, **_kwargs: ReleaseTag(name="v1.2.3", sha="a" * 40),
    )
    monkeypatch.setattr(
        mcp_server,
        "get_quality_report",
        lambda *_args, **_kwargs: {
            "quality_gate_status": "PASSED",
            "policy_version": "oss-v2",
        },
    )
    monkeypatch.setattr(mcp_server.deployment_store, "get", lambda _: item)
    monkeypatch.setattr(
        mcp_server.deployment_store,
        "list_for_service_with_total",
        lambda *_args, **_kwargs: ([item], 1),
    )
    monkeypatch.setattr(
        mcp_server.deployment_commands,
        "start_deployment",
        lambda **_kwargs: item,
    )
    monkeypatch.setattr(
        mcp_server.deployment_commands,
        "start_rollback",
        lambda **_kwargs: item,
    )
    monkeypatch.setattr(
        mcp_server.costs,
        "get_cost_summary",
        lambda *_args, **_kwargs: {"total_cost": 1.25},
    )

    async def metrics(_window):
        return {"services": []}

    monkeypatch.setattr(mcp_server.metrics, "get_cloud_run_metrics", metrics)
    token = auth_context_var.set(
        AuthenticatedUser(
            AccessToken(
                token="opaque",
                client_id="client",
                scopes=[
                    "eng-platform.read",
                    "eng-platform.deploy",
                    "eng-platform.rollback",
                ],
                subject="diegomad14",
            )
        )
    )
    try:
        assert (
            mcp_server.get_service("eng-platform-api")["service_name"]
            == "eng-platform-api"
        )
        assert (
            mcp_server.get_service_health("eng-platform-api")["service_name"]
            == "eng-platform-api"
        )
        assert (
            mcp_server.list_eligible_tags("eng-platform-api")["items"][0]["name"]
            == "v1.2.3"
        )
        assert (
            mcp_server.get_release_evidence("eng-platform-api", "v1.2.3")[
                "policy_version"
            ]
            == "oss-v2"
        )
        assert "recent" in mcp_server.list_releases()
        assert mcp_server.get_deployment("deployment-1")["id"] == "deployment-1"
        assert mcp_server.list_deployments("eng-platform-api")["total"] == 1
        assert (
            mcp_server.start_deployment(
                "eng-platform-api", "v1.2.3", "approved", "deploy-key"
            )["id"]
            == "deployment-1"
        )
        assert (
            mcp_server.start_rollback(
                "eng-platform-api", "deployment-1", "recover", "rollback-key"
            )["id"]
            == "deployment-1"
        )
        assert mcp_server.get_cost_summary()["total_cost"] == 1.25
        assert (await mcp_server.get_metrics_summary())["services"] == []
    finally:
        auth_context_var.reset(token)
    assert {record["tool"] for record in mcp_store._memory["audit"].values()} >= {
        "start_deployment",
        "start_rollback",
        "get_cost_summary",
        "get_metrics_summary",
    }


@pytest.mark.asyncio
async def test_oauth_provider_rejects_wrong_client_and_revokes_session():
    client = _client()
    other_client = _client("other-client")
    await provider.register_client(client)
    await provider.register_client(other_client)
    issued = await provider._issue_tokens(
        client_id="mcp-test",
        scopes=["eng-platform.read"],
        subject="diegomad14",
        resource=None,
    )
    assert await provider.load_refresh_token(other_client, issued.refresh_token) is None
    await provider.revoke_token(issued.refresh_token)
    assert await provider.verify_token(issued.access_token) is None
