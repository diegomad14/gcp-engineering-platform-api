"""MCP uses the same immutable capture/worker engine, isolated by OAuth grant."""

import asyncio
import json
import time
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from fastapi import HTTPException, Request
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.shared.auth import OAuthClientInformationFull

from eng_platform_api import mcp_server as mcp
from eng_platform_api.config import config
from eng_platform_api.services import database_jobs as jobs, database_views as views
from eng_platform_api.services import (
    database_console as console,
    database_sessions as sessions,
)
from eng_platform_api.services import mcp_auth, mcp_store, mcp_grants
from eng_platform_api.services.database_registry import DatabaseUnavailable
from tests.test_database_jobs import configured as configured
from tests.mcp_helpers import access_token
from tests.test_release_authorization import _private_key_pem, _expected
from eng_platform_api.services import release_authorization, release_authorization_store
from eng_platform_api.main import app
from fastapi.testclient import TestClient


@pytest.fixture
def environment(configured, monkeypatch):
    monkeypatch.setattr(config.mcp, "enabled", True)
    monkeypatch.setattr(config.mcp, "public_base_url", "https://api.example")
    monkeypatch.setattr(mcp_store, "_collection", lambda _: None)
    monkeypatch.setattr(
        mcp_store,
        "save_audit",
        lambda value: mcp_store._memory["audit"].update(
            {str(len(mcp_store._memory["audit"])): value}
        ),
    )
    for records in mcp_store._memory.values():
        records.clear()
    return configured


@contextmanager
def actor(token=None, *, login="outside-allowlists", client="client"):
    value = token or access_token(subject=login, client_id=client)
    context = auth_context_var.set(AuthenticatedUser(value))
    try:
        yield mcp._principal()
    finally:
        auth_context_var.reset(context)


def capture(environment, monkeypatch, *, rows=None):
    rows = (
        rows
        if rows is not None
        else [[i, str(i) + ".1234567890123456789"] for i in range(601)]
    )

    def stream(
        database, statement, authorize, on_columns, on_rows, *, admission, **kwargs
    ):
        with admission:
            authorize()
            on_columns(
                [
                    {"key": "0", "name": "duplicate", "data_type": "int8"},
                    {"key": "1", "name": "duplicate", "data_type": "numeric"},
                ]
            )
            for start in range(0, len(rows), 16):
                on_rows(rows[start : start + 16])
        return {"row_count": len(rows), "elapsed_ms": 1}

    run = Mock(side_effect=stream)
    monkeypatch.setattr(console, "stream_query", run)
    workspace = mcp.create_database_workspace("sample")["workspace_id"]
    query = mcp.start_database_query("sample", workspace, "SELECT 1", "query-1")
    assert (
        mcp.start_database_query("sample", workspace, "SELECT 1", "query-1")[
            "execution_id"
        ]
        == query["execution_id"]
    )
    jobs.run_query(query["execution_id"])
    assert (
        mcp.get_database_query("sample", workspace, query["execution_id"])["status"]
        == "completed"
    )
    return workspace, query["execution_id"], run


def test_all_rows_sorted_pages_single_query_and_private_audit(environment, monkeypatch):
    with actor():
        inventory = mcp.list_databases()
        assert inventory["databases"][0]["id"] == "sample"
        assert inventory["databases"][0]["max_rows"] is None
        assert inventory["databases"][0]["timeout_seconds"] == 240
        assert inventory["workspace_limits"]["timeout_seconds"] == 240
        workspace, execution, run = capture(environment, monkeypatch)
        original = mcp.get_database_page("sample", workspace, execution)
        assert len(original["rows"]) == 25
        selected = mcp.create_database_view(
            "sample", workspace, execution, "0", "desc", "sort-1"
        )
        assert (
            mcp.create_database_view(
                "sample", workspace, execution, "0", "desc", "sort-1"
            )["view_id"]
            == selected["view_id"]
        )
        views.run_sort(selected["view_id"])
        assert (
            mcp.get_database_view("sample", workspace, execution, selected["view_id"])[
                "status"
            ]
            == "completed"
        )
        rows = []
        for index in range(7):
            rows.extend(
                mcp.get_database_page(
                    "sample", workspace, execution, index, 100, selected["view_id"]
                )["rows"]
            )
        assert [row[0] for row in rows] == list(range(600, -1, -1))
        assert rows[0][1] == "600.1234567890123456789"
        assert mcp.get_database_page("sample", workspace, execution) == original
        mcp.cancel_database_view("sample", workspace, execution, selected["view_id"])
        assert mcp.get_database_page("sample", workspace, execution) == original
        mcp.purge_database_workspace("sample", workspace)
        with pytest.raises(HTTPException):
            mcp.get_database_page("sample", workspace, execution)
    run.assert_called_once()
    audit = json.dumps(mcp_store._memory["audit"])
    assert "SELECT" not in audit and "600.123" not in audit
    assert all(not item["mutation"] for item in mcp_store._memory["audit"].values())


def test_schema_uses_shared_metadata_permit_and_preserves_types(
    environment, monkeypatch
):
    def schema(database, authorize, *, admission):
        with admission:
            authorize()
            return {
                "tables": [
                    {
                        "name": "long_table",
                        "services": ["api"],
                        "columns": [{"name": "id", "data_type": "int8"}],
                    }
                ]
            }

    monkeypatch.setattr(console, "read_schema", schema)
    with actor():
        assert (
            mcp.get_database_schema("sample")["tables"][0]["columns"][0]["data_type"]
            == "int8"
        )
        with pytest.raises(HTTPException) as error:
            mcp.get_database_schema("missing")
        assert error.value.status_code == 404


def test_same_user_different_connections_and_browser_cannot_share_capture(
    environment, monkeypatch
):
    with actor(token=access_token(token="one", subject="reader", client_id="one")):
        workspace, execution, _ = capture(environment, monkeypatch)
    with actor(token=access_token(token="two", subject="reader", client_id="two")):
        with pytest.raises(HTTPException) as error:
            mcp.get_database_query("sample", workspace, execution)
        assert error.value.status_code == 404
    with pytest.raises(HTTPException):
        jobs.execution(environment["request"], "sample", workspace, execution)
    grant = next(
        r for r in environment["control"].records.values() if r.get("source") == "mcp"
    )
    monkeypatch.setattr(sessions, "_session_id", lambda _: grant["id"])
    with pytest.raises(HTTPException):
        sessions.require(
            Request(
                {
                    "type": "http",
                    "session": {
                        "github_auth_provider": "github_oauth",
                        "github_login": "reader",
                    },
                    "headers": [],
                }
            )
        )


@pytest.mark.asyncio
async def test_rotation_keeps_capture_and_revoking_old_credential_revokes_family(
    environment, monkeypatch
):
    issued = await mcp_auth.provider._issue_tokens(
        client_id="client",
        scopes=[mcp_grants.SCOPE],
        subject="outside-allowlists",
        resource=None,
    )
    first = await mcp_auth.provider.load_access_token(issued.access_token)
    with actor(first) as principal:
        workspace, execution, _ = capture(environment, monkeypatch)
        expiry = mcp.get_database_query("sample", workspace, execution)["expires_at"]
        grant_id = principal.id
    client = OAuthClientInformationFull(
        client_id="client",
        redirect_uris=["https://client.example/callback"],
        token_endpoint_auth_method="none",
    )
    refresh = await mcp_auth.provider.load_refresh_token(client, issued.refresh_token)
    rotated = await mcp_auth.provider.exchange_refresh_token(
        client, refresh, [mcp_grants.SCOPE]
    )
    assert await mcp_auth.provider.load_access_token(issued.access_token) is None
    assert (
        await mcp_auth.provider.load_refresh_token(client, issued.refresh_token) is None
    )
    with actor(
        await mcp_auth.provider.load_access_token(rotated.access_token)
    ) as principal:
        assert principal.id == grant_id
        assert (
            mcp.get_database_query("sample", workspace, execution)["expires_at"]
            == expiry
        )
        assert len(mcp.get_database_page("sample", workspace, execution)["rows"]) == 25
    await mcp_auth.provider.revoke_token(issued.access_token)
    assert await mcp_auth.provider.load_access_token(rotated.access_token) is None
    assert (
        await mcp_auth.provider.load_refresh_token(client, rotated.refresh_token)
        is None
    )
    assert environment["control"].get("workspace", workspace)["state"] == "purged"


@pytest.mark.asyncio
async def test_refresh_compare_and_swap_prevents_two_live_generations(environment):
    issued = await mcp_auth.provider._issue_tokens(
        client_id="client", scopes=[mcp_grants.SCOPE], subject="reader", resource=None
    )
    client = OAuthClientInformationFull(
        client_id="client",
        redirect_uris=["https://client.example/callback"],
        token_endpoint_auth_method="none",
    )
    refresh = await mcp_auth.provider.load_refresh_token(client, issued.refresh_token)
    results = await asyncio.gather(
        *[
            mcp_auth.provider.exchange_refresh_token(
                client, refresh, [mcp_grants.SCOPE]
            )
            for _ in range(2)
        ],
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1


def test_revocation_at_finish_cannot_publish_and_cleans_objects(
    environment, monkeypatch
):
    with actor() as principal:

        def stream(
            database, statement, authorize, on_columns, on_rows, *, admission, **kwargs
        ):
            with admission:
                on_columns([{"key": "0", "name": "id", "data_type": "int8"}])
                on_rows([[1]])
                mcp_grants.revoke(principal.id)
            return {"row_count": 1, "elapsed_ms": 1}

        monkeypatch.setattr(console, "stream_query", stream)
        workspace = mcp.create_database_workspace("sample")["workspace_id"]
        query = mcp.start_database_query("sample", workspace, "SELECT 1", "query")
        jobs.run_query(query["execution_id"])
        record = environment["control"].get("execution", query["execution_id"])
        assert record["state"] != "completed" and not record.get("manifest")
        assert not environment["objects"].contains(query["execution_id"])


def test_pending_cancel_invalid_inputs_expiry_and_disabled_policy(environment):
    with actor() as principal:
        workspace = mcp.create_database_workspace("sample")["workspace_id"]
        query = mcp.start_database_query("sample", workspace, "SELECT 1", "query")
        assert (
            mcp.get_database_query("sample", workspace, query["execution_id"])["status"]
            == "queued"
        )
        mcp.cancel_database_query("sample", workspace, query["execution_id"])
        assert (
            mcp.get_database_query("sample", workspace, query["execution_id"])["status"]
            == "cancelled"
        )
        with pytest.raises(HTTPException):
            mcp.start_database_query(
                "sample", workspace, "DELETE FROM sample.table", "bad"
            )
        environment["control"].records[f"session:{principal.id}"]["expires_at"] = (
            time.time() - 1
        )
        with pytest.raises(HTTPException):
            mcp.get_database_query("sample", workspace, query["execution_id"])


def test_mcp_release_capability_is_signed_and_rechecked_at_consumption(
    environment, monkeypatch
):
    monkeypatch.setattr(
        config.github, "release_signing_private_key", _private_key_pem()
    )
    monkeypatch.setattr(
        release_authorization_store, "consume", lambda *args, **kwargs: True
    )
    expected = _expected()
    monkeypatch.setattr(config, "mock_mode", True)
    with actor() as principal, mcp_grants.authority(principal):
        token, claims = release_authorization.issue(
            **{
                k: expected[k]
                for k in ("repository", "service_name", "tag", "sha", "kind")
            },
            github_deployment_id=123,
            requested_by=principal.login,
        )
    assert release_authorization.verify(token, expected)["mcp_grant_id"] == principal.id
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/internal/release-authorizations/consume",
                json={"token": token, **expected},
            ).status_code
            == 200
        )
        mcp_grants.revoke(principal.id)
        assert (
            client.post(
                "/api/internal/release-authorizations/consume",
                json={"token": token, **expected},
            ).status_code
            == 401
        )
    assert claims["mcp_authority_policy"] == mcp_grants.POLICY


def test_control_storage_failure_and_audit_do_not_leak_sql(environment, monkeypatch):
    with actor():
        monkeypatch.setattr(
            jobs,
            "active_policy",
            lambda: (_ for _ in ()).throw(
                DatabaseUnavailable("private provider detail")
            ),
        )
        with pytest.raises(HTTPException) as error:
            mcp.list_databases()
        assert error.value.status_code == 503 and "private" not in error.value.detail
    assert next(iter(mcp_store._memory["audit"].values()))["result"] == "error"


def test_invalid_payload_is_sanitized_before_mcp_error(environment):
    with actor():
        workspace = mcp.create_database_workspace("sample")["workspace_id"]
        with pytest.raises(HTTPException) as error:
            mcp.start_database_query(
                "sample", workspace, "private sql" * 4000, "invalid request!"
            )
        assert error.value.status_code == 422 and "private sql" not in str(error.value)
        with pytest.raises(HTTPException):
            mcp.get_database_page("sample", workspace, "missing", -1)


def test_unified_notification_preserves_fixed_destination_and_web_owner_check(
    environment, monkeypatch
):
    from eng_platform_api.services import cost_alerts
    from eng_platform_api.config import CostAlertsConfig

    settings = CostAlertsConfig(
        enabled=True,
        allowed_logins=(cost_alerts.OWNER_LOGIN,),
        recipient_owner_login=cost_alerts.OWNER_LOGIN,
        recipient_id=cost_alerts.RECIPIENT_ID,
        private_destination_confirmed=True,
        recipient_address="123457589",
        communications_api_key="synthetic",
        maximum_export_age_hours=48,
    )
    monkeypatch.setattr(config, "cost_alerts", settings)
    monkeypatch.setattr(config.mcp, "oauth_collection", "test-oauth")
    monkeypatch.setattr(config.mcp, "audit_collection", "test-audit")
    with actor() as principal:
        cost_alerts._authorized(principal.login, mcp_authority=principal)
        with pytest.raises(HTTPException) as error:
            cost_alerts._authorized(principal.login)
        assert error.value.status_code == 403
        settings.recipient_owner_login = "outside-allowlists"
        with pytest.raises(HTTPException) as error:
            cost_alerts._authorized(principal.login, mcp_authority=principal)
        assert error.value.status_code == 409


def test_forged_or_changed_grants_are_denied(environment):
    with actor() as principal:
        grant = environment["control"].records[f"session:{principal.id}"]
        grant["authority_policy"] = "other-policy"
        with pytest.raises(HTTPException):
            mcp.list_databases()
        grant["authority_policy"] = mcp_grants.POLICY
        bad = mcp_grants.Principal(
            principal.id,
            principal.login,
            "wrong-client",
            principal.resource,
            principal.generation,
        )
        with pytest.raises(HTTPException):
            jobs.create_workspace(bad, "sample")
        with mcp_grants.authority(principal):
            with pytest.raises(HTTPException):
                mcp_grants.release_claims("other-user")
        with pytest.raises(HTTPException):
            mcp_grants.resource("https://other.example/mcp")


def test_oauth_storage_has_no_production_memory_fallback(monkeypatch):
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.mcp, "oauth_collection", "")
    with pytest.raises(RuntimeError, match="Persistent"):
        mcp_store.get("access", "some-token-hash")


@pytest.mark.asyncio
async def test_transaction_retry_does_not_return_inactive_refresh_pair(
    environment, monkeypatch
):
    from eng_platform_api.services import database_job_store
    from mcp.server.auth.provider import TokenError

    issued = await mcp_auth.provider._issue_tokens(
        client_id="client", scopes=[mcp_grants.SCOPE], subject="reader", resource=None
    )
    record = mcp_auth.provider._credential("refresh", issued.refresh_token)
    original = database_job_store.mutate

    def retry(references, transform):
        key = f"session:{record['session_id']}"
        value = environment["control"].get("session", record["session_id"])
        transform({key: value})  # Aborted CAS attempt.
        value["generation"] += 1
        transform({key: value})  # A winning concurrent rotation changed generation.

    monkeypatch.setattr(database_job_store, "mutate", retry)
    with pytest.raises(TokenError):
        await mcp_auth.provider._issue_tokens(
            client_id="client",
            scopes=[mcp_grants.SCOPE],
            subject="reader",
            resource=None,
            grant_id=record["session_id"],
            previous_refresh=mcp_store.token_key(issued.refresh_token),
        )
    monkeypatch.setattr(database_job_store, "mutate", original)
    assert (
        len(mcp_store._memory["access"]) == 1 and len(mcp_store._memory["refresh"]) == 1
    )
