"""Bootstrap API/MCP authorization and public surface, without external effects."""

from unittest.mock import Mock

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

from eng_platform_api import mcp_server
from eng_platform_api.config import config
from eng_platform_api.main import app
from eng_platform_api.routers import release_operations
from eng_platform_api.services import database_job_store, mcp_store
from tests.mcp_helpers import Control, access_token

EXECUTION = "a" * 64
PUBLIC = {
    "execution_id": EXECUTION,
    "status": "unknown",
    "provider": "cloud_build",
    "build_id": "",
    "provider_run_id": "",
    "logs_url": "",
}


@pytest.fixture
def mcp_operator(monkeypatch):
    monkeypatch.setattr(config.mcp, "enabled", True)
    monkeypatch.setattr(config.mcp, "public_base_url", "http://testserver")
    for values in mcp_store._memory.values():
        values.clear()
    with database_job_store.testing_backend(Control()):
        token = auth_context_var.set(AuthenticatedUser(access_token()))
        try:
            yield
        finally:
            auth_context_var.reset(token)


def test_http_bootstrap_requires_authenticated_deployer(monkeypatch):
    monkeypatch.setattr(config, "mock_mode", False)
    submit = Mock()
    monkeypatch.setattr(
        release_operations.release_quality_bootstrap,
        "request_quality_bootstrap",
        submit,
    )
    with TestClient(app) as client:
        result = client.post(
            f"/api/internal/release-operations/executions/{EXECUTION}/quality-bootstrap",
            json={"idempotency_key": "approved"},
        )
    assert result.status_code == 401
    submit.assert_not_called()


def test_http_bootstrap_forbids_empty_allowlist_and_arbitrary_build_fields(monkeypatch):
    submit = Mock()
    monkeypatch.setattr(
        release_operations.release_quality_bootstrap,
        "request_quality_bootstrap",
        submit,
    )
    with TestClient(app) as client:
        monkeypatch.setattr(config.auth, "allowed_logins", ())
        denied = client.post(
            f"/api/internal/release-operations/executions/{EXECUTION}/quality-bootstrap",
            json={"idempotency_key": "approved"},
        )
        assert denied.status_code == 403
        monkeypatch.setattr(config.auth, "allowed_logins", ("diegomad14",))
        arbitrary = client.post(
            f"/api/internal/release-operations/executions/{EXECUTION}/quality-bootstrap",
            json={"idempotency_key": "approved", "timeout": "7200s"},
        )
        assert arbitrary.status_code == 422
        malformed_id = client.post(
            "/api/internal/release-operations/executions/not-an-execution/quality-bootstrap",
            json={"idempotency_key": "approved"},
        )
        assert malformed_id.status_code == 422
    submit.assert_not_called()


def test_http_bootstrap_reauthorizes_live_allowlist(monkeypatch):
    request = Request({"type": "http", "headers": [], "session": {}})

    def submit(execution_id, *, idempotency_key, actor, reauthorize):
        assert (execution_id, idempotency_key, actor) == (
            EXECUTION,
            "approved",
            "diegomad14",
        )
        reauthorize()
        monkeypatch.setattr(config.auth, "allowed_logins", ())
        reauthorize()
        pytest.fail("Revocation must stop submission")

    monkeypatch.setattr(
        release_operations.release_quality_bootstrap,
        "request_quality_bootstrap",
        submit,
    )
    with pytest.raises(HTTPException) as error:
        release_operations.request_quality_bootstrap(
            EXECUTION,
            release_operations.QualityBootstrapRequest(idempotency_key="approved"),
            request,
            "diegomad14",
        )
    assert error.value.status_code == 403


def test_mcp_bootstrap_public_status_and_sanitized_mutation_audit(
    mcp_operator, monkeypatch
):
    def submit(execution_id, *, idempotency_key, actor, reauthorize):
        assert execution_id == EXECUTION and actor == "diegomad14"
        assert idempotency_key == "approved"
        reauthorize()
        return PUBLIC

    monkeypatch.setattr(
        mcp_server.release_quality_bootstrap, "request_quality_bootstrap", submit
    )
    assert mcp_server.request_quality_bootstrap(EXECUTION, "approved") == PUBLIC
    audit = next(iter(mcp_store._memory["audit"].values()))
    assert audit["tool"] == "request_quality_bootstrap"
    assert audit["mutation"] is True and audit["result"] == "accepted"
    assert len(audit["input_fingerprint"]) == 64
    assert "approved" not in str(audit) and "opaque" not in str(audit)


@pytest.mark.parametrize("revoke", ["credential", "allowlist"])
def test_mcp_bootstrap_live_revocation_stops_before_post(
    mcp_operator, monkeypatch, revoke
):
    def submit(execution_id, *, idempotency_key, actor, reauthorize):
        reauthorize()
        if revoke == "credential":
            monkeypatch.setattr(mcp_server.provider, "_credential", lambda *_: None)
        else:
            monkeypatch.setattr(config.auth, "allowed_logins", ())
        reauthorize()
        pytest.fail("Revocation must stop submission")

    monkeypatch.setattr(
        mcp_server.release_quality_bootstrap, "request_quality_bootstrap", submit
    )
    with pytest.raises(HTTPException) as error:
        mcp_server.request_quality_bootstrap(EXECUTION, "approved")
    assert error.value.status_code in {401, 403}
    audit = next(iter(mcp_store._memory["audit"].values()))
    assert audit["result"] == "error" and "opaque" not in str(audit)


def test_mcp_bootstrap_enforces_rate_limit_and_empty_allowlist(
    mcp_operator, monkeypatch
):
    submit = Mock()
    monkeypatch.setattr(
        mcp_server.release_quality_bootstrap, "request_quality_bootstrap", submit
    )
    monkeypatch.setattr(config.auth, "allowed_logins", ())
    with pytest.raises(HTTPException) as denied:
        mcp_server.request_quality_bootstrap(EXECUTION, "approved")
    assert denied.value.status_code == 403
    monkeypatch.setattr(config.auth, "allowed_logins", ("diegomad14",))
    monkeypatch.setattr(config.mcp, "mutation_limit_per_hour", 0)
    with pytest.raises(HTTPException) as limited:
        mcp_server.request_quality_bootstrap(EXECUTION, "approved")
    assert limited.value.status_code == 429
    submit.assert_not_called()


@pytest.mark.parametrize("execution,key", [("b" * 40, "approved"), (EXECUTION, " ")])
def test_mcp_bootstrap_rejects_invalid_input_before_service(
    monkeypatch, execution, key
):
    submit = Mock()
    monkeypatch.setattr(
        mcp_server.release_quality_bootstrap, "request_quality_bootstrap", submit
    )
    with pytest.raises(HTTPException) as error:
        mcp_server.request_quality_bootstrap(execution, key)
    assert error.value.status_code == 422
    submit.assert_not_called()


def test_mcp_bootstrap_errors_never_expose_internal_nonce(mcp_operator, monkeypatch):
    monkeypatch.setattr(
        mcp_server.release_quality_bootstrap,
        "request_quality_bootstrap",
        Mock(side_effect=RuntimeError("credential secret nonce-internal")),
    )
    with pytest.raises(HTTPException) as error:
        mcp_server.request_quality_bootstrap(EXECUTION, "approved")
    assert error.value.status_code == 503
    assert "nonce" not in error.value.detail and "secret" not in error.value.detail


@pytest.mark.asyncio
async def test_mcp_bootstrap_is_registered_with_fixed_schema_and_current_scope():
    tools = await mcp_server.mcp.list_tools()
    tool = next(tool for tool in tools if tool.name == "request_quality_bootstrap")
    assert set(tool.inputSchema["properties"]) == {"execution_id", "idempotency_key"}
    assert tool.meta["securitySchemes"] == [
        {"type": "oauth2", "scopes": ["eng-platform.access"]}
    ]
