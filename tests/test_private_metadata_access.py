"""Private catalog metadata never bypasses live reader ACLs through HTTP or MCP."""

from copy import deepcopy
import hashlib
from unittest.mock import Mock

from fastapi import HTTPException
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
import pytest

from eng_platform_api import mcp_server
from eng_platform_api.config import catalog_source_identity, config
from eng_platform_api.models import DeploymentOverview, DeploymentOverviewItem
from eng_platform_api.routers import deployments, quality
from eng_platform_api.services import catalog, log_catalog, runtime_logs
from tests.test_private_catalog_source import (
    PRIVATE,
    catalog_client,
    encode,
    install_private,
)


@pytest.fixture(autouse=True)
def private_authority(monkeypatch, tmp_path):
    path = install_private(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.logs, "enabled", False)
    monkeypatch.setattr(config.logs, "allowed_logins", ("demo-reader",))
    monkeypatch.setattr(
        config.auth, "session_secret", "synthetic-session-secret-32-characters"
    )
    monkeypatch.setattr(config.auth, "github_client_id", "synthetic-client")
    monkeypatch.setattr(config.auth, "github_client_secret", "synthetic-secret")
    monkeypatch.setattr(mcp_server, "_audit", Mock())
    denied = Mock(
        side_effect=AssertionError(
            "Metadata must not query logs or discover a denied resource"
        )
    )
    monkeypatch.setattr(catalog, "_run_client", denied)
    monkeypatch.setattr(catalog, "_job_client", denied)
    monkeypatch.setattr(runtime_logs, "_logging_client", denied)
    yield path
    denied.assert_not_called()


def replace_authority(monkeypatch, path, document):
    raw = encode(document)
    path.write_bytes(raw)
    monkeypatch.setattr(config, "catalog_sha256", hashlib.sha256(raw).hexdigest())


@pytest.mark.parametrize(
    "path",
    [
        "/api/catalog/services",
        "/api/catalog/services/private-example",
        "/api/deployments/overview",
        "/api/deployments/synthetic-deployment",
        "/api/services/private-example/tags",
        "/api/services/private-example/deployments",
        "/api/metrics/cloud-run/summary",
        "/api/health/services",
        "/api/costs/status",
        "/api/costs/summary",
        "/api/costs/by-service",
        "/api/costs/by-sku",
        "/api/costs/daily",
        "/api/costs/comparison",
        "/api/releases/",
        "/api/releases/summary",
        "/api/releases/private-example/latest",
        "/api/quality/summary",
        "/api/quality/services/private-example/reports",
    ],
)
def test_anonymous_metadata_is_denied_before_sources_or_cache(monkeypatch, path):
    denied = Mock(
        side_effect=AssertionError("Anonymous metadata must not load authority")
    )
    monkeypatch.setattr(log_catalog, "resources", denied)
    response = catalog_client(login=None).get(path)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
    assert "private-example" not in response.text
    denied.assert_not_called()


@pytest.mark.parametrize(
    ("login", "provider", "status"),
    [
        ("unapproved-reader", "github_oauth", 403),
        ("demo-reader", "mock", 401),
        ("demo-reader", "", 401),
        ("diegomad14", "github_oauth", 403),
    ],
)
def test_only_real_approved_reader_can_view_private_catalog(login, provider, status):
    assert (
        catalog_client(login, provider).get("/api/catalog/services").status_code
        == status
    )


def test_iap_or_bearer_headers_do_not_impersonate_oauth_session(monkeypatch):
    monkeypatch.setattr(config.auth, "trust_iap_identity", True)
    response = catalog_client(login=None).get(
        "/api/catalog/services",
        headers={
            "X-Goog-Authenticated-User-Email": "accounts.google.com:demo-reader",
            "Authorization": "Bearer synthetic-not-a-session",
        },
    )
    assert response.status_code == 401


def test_metadata_capability_is_independent_of_logs_and_deployment():
    client = catalog_client()
    session = client.get("/api/auth/me").json()
    assert session["can_view_catalog"] is True
    assert session["can_view_logs"] is False
    assert session["can_deploy"] is False
    response = client.get("/api/catalog/services")
    assert response.status_code == 200
    assert response.json()["services"][0]["service_name"] == "private-example"
    assert "allowed_logins" not in response.text and "demo-reader" not in response.text
    assert client.post("/api/auth/logout").json()["can_view_catalog"] is False
    assert client.get("/api/catalog/services").status_code == 401


def test_closed_future_resource_is_filtered_before_readiness_without_hiding_current(
    monkeypatch, private_authority
):
    document = deepcopy(PRIVATE)
    hidden = deepcopy(document["services"][0])
    hidden["service_name"] = "future-disabled"
    hidden["logs"] = {"enabled": False, "allowed_logins": []}
    document["services"].append(hidden)
    replace_authority(monkeypatch, private_authority, document)
    original = catalog._catalog_service
    seen = []

    def project(row):
        seen.append(row["service_name"])
        return original(row)

    monkeypatch.setattr(catalog, "_catalog_service", project)
    client = catalog_client()
    response = client.get("/api/catalog/services")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert seen == ["private-example"]
    assert "future-disabled" not in response.text
    assert client.get("/api/catalog/services/future-disabled").status_code == 403
    assert client.get("/api/deployments/overview").status_code == 403
    assert client.get("/api/auth/me").json()["can_view_catalog"] is True


def test_global_and_resource_revocation_precede_warm_aggregate_cache(
    monkeypatch, private_authority
):
    cached = DeploymentOverview(
        items=[DeploymentOverviewItem(service_name="private-example")]
    )
    monkeypatch.setattr(
        deployments,
        "_overview_cache",
        (deployments.monotonic(), cached, catalog_source_identity()),
    )
    client = catalog_client()
    assert client.get("/api/deployments/overview").status_code == 200
    monkeypatch.setattr(config.logs, "allowed_logins", ())
    assert client.get("/api/deployments/overview").status_code == 403
    monkeypatch.setattr(config.logs, "allowed_logins", ("demo-reader",))
    document = deepcopy(PRIVATE)
    document["services"][0]["logs"]["allowed_logins"] = []
    replace_authority(monkeypatch, private_authority, document)
    assert client.get("/api/deployments/overview").status_code == 403
    assert client.get("/api/auth/me").json()["can_view_catalog"] is False


@pytest.mark.parametrize("failure", ["tamper", "partial", "missing"])
def test_bad_private_authority_stays_closed_even_with_logs_off(
    monkeypatch, private_authority, failure
):
    if failure == "tamper":
        private_authority.write_bytes(b"{}")
    elif failure == "partial":
        monkeypatch.setattr(config, "catalog_sha256", None)
    else:
        private_authority.unlink()
    client = catalog_client()
    assert client.get("/api/catalog/services").status_code == 503
    assert client.get("/api/auth/me").json()["can_view_catalog"] is False
    assert client.get("/health").status_code == 200


def test_machine_quality_evidence_remains_reachable_without_browser_session(
    monkeypatch,
):
    get_report = Mock(return_value=None)
    monkeypatch.setattr(quality.quality_store, "get_report", get_report)
    response = catalog_client(login=None).get(
        "/api/quality/services/private-example/commits/"
        + "a" * 40
        + "?for_release=true"
    )
    assert response.status_code == 404
    get_report.assert_called_once()


def mcp_context(login="demo-reader", scopes=None):
    return auth_context_var.set(
        AuthenticatedUser(
            AccessToken(
                token="synthetic-context",
                client_id="synthetic-client",
                subject=login,
                scopes=["eng-platform.read"] if scopes is None else scopes,
            )
        )
    )


def test_mcp_requires_current_approved_principal_and_filters_catalog(
    monkeypatch, private_authority
):
    token = mcp_context()
    try:
        assert mcp_server.list_services()["total"] == 1
        monkeypatch.setattr(config.logs, "allowed_logins", ())
        with pytest.raises(HTTPException) as error:
            mcp_server.list_services()
        assert error.value.status_code == 403
        monkeypatch.setattr(config.logs, "allowed_logins", ("demo-reader",))
        document = deepcopy(PRIVATE)
        hidden = deepcopy(document["services"][0])
        hidden["service_name"] = "future-disabled"
        hidden["logs"] = {"enabled": False, "allowed_logins": []}
        document["services"].append(hidden)
        replace_authority(monkeypatch, private_authority, document)
        assert mcp_server.list_services()["total"] == 1
        with pytest.raises(HTTPException) as error:
            mcp_server.get_service("future-disabled")
        assert error.value.status_code == 403
    finally:
        auth_context_var.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("login", "scopes"),
    [("unapproved-reader", ["eng-platform.read"]), ("demo-reader", [])],
)
async def test_mcp_metrics_cannot_bypass_private_reader_scope(login, scopes):
    token = mcp_context(login, scopes)
    try:
        with pytest.raises(HTTPException) as error:
            await mcp_server.get_metrics_summary()
        assert error.value.status_code == 403
    finally:
        auth_context_var.reset(token)


@pytest.mark.parametrize(
    "suffix",
    [
        "commits/" + "a" * 40 + "?for_release=true",
        "rollback-targets/synthetic-revision",
    ],
)
def test_private_inventory_and_unknown_exact_evidence_are_indistinguishable(
    monkeypatch, private_authority, suffix
):
    from tests.test_observability_catalog import observation

    document = deepcopy(PRIVATE)
    document["services"].append(observation("private-observation"))
    replace_authority(monkeypatch, private_authority, document)
    # Even an orphan persisted report must never turn inventory into evidence.
    store = Mock(
        side_effect=AssertionError(
            "Do not query evidence for private inventory or unknown names"
        )
    )
    monkeypatch.setattr(quality.quality_store, "get_report", store)
    responses = [
        catalog_client(login=None).get(f"/api/quality/services/{name}/{suffix}")
        for name in ("private-observation", "unknown-resource")
    ]
    assert [response.status_code for response in responses] == [404, 404]
    assert (
        responses[0].json()
        == responses[1].json()
        == {"detail": "Quality report not found"}
    )
    store.assert_not_called()


def test_overview_cache_is_bound_to_source_pin_and_rejects_inflight_old_generation(
    monkeypatch, private_authority
):
    document = deepcopy(PRIVATE)
    removed = deepcopy(document["services"][0])
    removed["service_name"] = "removed-example"
    document["services"].append(removed)
    replace_authority(monkeypatch, private_authority, document)
    old_source = catalog_source_identity()
    monkeypatch.setattr(deployments, "_overview_cache", None)
    monkeypatch.setattr(
        deployments.deployment_store, "latest_for_services", lambda names: {}
    )
    switched = []

    def delayed_projection(service, previous):
        if not switched:
            switched.append(True)
            replace_authority(monkeypatch, private_authority, PRIVATE)
        return DeploymentOverviewItem(service_name=service.service_name)

    monkeypatch.setattr(deployments, "_overview_item", delayed_projection)
    client = catalog_client()
    response = client.get("/api/deployments/overview")
    assert response.status_code == 503
    assert "removed-example" not in response.text
    assert deployments._overview_cache[2] == old_source
    assert old_source != catalog_source_identity()
    response = client.get("/api/deployments/overview")
    assert response.status_code == 200
    assert [item["service_name"] for item in response.json()["items"]] == [
        "private-example"
    ]
    assert deployments._overview_cache[2] == catalog_source_identity()


def test_mcp_rechecks_private_policy_after_provider_io(monkeypatch):
    token = mcp_context()

    def late_response():
        monkeypatch.setattr(config.logs, "allowed_logins", ())
        return {"private_result": "must-not-return"}

    try:
        with pytest.raises(HTTPException) as error:
            mcp_server._read(
                "get_service", {"service_name": "private-example"}, late_response
            )
        assert error.value.status_code == 403
    finally:
        auth_context_var.reset(token)


def test_private_factory_plan_cannot_probe_resource_existence_before_auth(monkeypatch):
    from eng_platform_api.routers import service_factory

    denied = Mock(
        side_effect=AssertionError(
            "Anonymous factory requests must not enter the handler"
        )
    )
    monkeypatch.setattr(service_factory.sf, "generate_plan", denied)
    client = catalog_client(login=None)
    responses = [
        client.post(
            "/api/service-factory/plan",
            json={
                "repository": "example/new-service",
                "service_name": name,
                "service_type": "api",
                "runtime": "python",
                "gcp_project": "example-project",
                "owner": "example-team",
            },
        )
        for name in ("private-example", "unknown-example")
    ]
    assert [response.status_code for response in responses] == [401, 401]
    assert responses[0].json() == responses[1].json()
    assert all(
        response.headers["cache-control"] == "no-store" for response in responses
    )
    denied.assert_not_called()
    assert client.get("/api/service-factory/templates").status_code == 200


def test_explicit_public_mock_factory_smoke_still_works(monkeypatch):
    monkeypatch.setattr(config, "mock_mode", True)
    monkeypatch.setattr(config, "catalog_path", None)
    monkeypatch.setattr(config, "catalog_sha256", None)
    response = catalog_client(login=None).post(
        "/api/service-factory/plan",
        json={
            "repository": "example/new-service",
            "service_name": "new-example",
            "service_type": "api",
            "runtime": "python",
            "gcp_project": "example-project",
            "owner": "example-team",
        },
    )
    assert response.status_code == 200
    assert "platform_deploy_workflow" in response.text
