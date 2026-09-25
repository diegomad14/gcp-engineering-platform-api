"""Tests for the flat service catalog."""

from fastapi.testclient import TestClient
from types import SimpleNamespace

from eng_platform_api.main import app
from eng_platform_api.services import catalog
from eng_platform_api.config import config

client = TestClient(app)


def test_list_services():
    response = client.get("/api/catalog/services")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 18
    assert len(data["services"]) == 18
    assert all("display_name" not in service for service in data["services"])
    names = {service["service_name"] for service in data["services"]}
    assert {"cgm-sanplat-api", "cgm-sanplat-web", "eng-platform-api"} <= names
    artemis = [
        service
        for service in data["services"]
        if service["service_name"].startswith("cgm-artemis-")
    ]
    assert len(artemis) == 12
    ready = {
        service["service_name"] for service in artemis if service["deployment_ready"]
    }
    # Every Artemis runtime now exists (services and Jobs), so all twelve are
    # deployable through the governed path. Triggers stay paused until cutover.
    assert ready == {service["service_name"] for service in artemis}
    assert all(not service["deployment_blockers"] for service in artemis)
    assert all(
        service["repository"].startswith("diegomad14/cgm-artemis-")
        for service in artemis
    )
    # The new API and Web runtimes stay private until the URL cutover, so the
    # deploy executor must smoke them with an identity token.
    assert all(
        service["deployment"]["private_runtime"]
        for service in artemis
        if service["service_name"] in {"cgm-artemis-api", "cgm-artemis-web"}
    )


def test_renamed_repositories_share_quality_evidence_both_ways():
    services = client.get("/api/catalog/services").json()["services"]
    by_name = {service["service_name"]: service for service in services}

    # The renamed repository publishes one execution per SHA: the Artemis entry
    # and its SanPlat alias must accept the same exact evidence, otherwise a
    # legacy deploy of the still-serving runtime can never be authorized.
    assert set(by_name["cgm-sanplat-api"]["quality"]["evidence_services"]) == {
        "cgm-artemis-api",
        "cgm-sanplat-api",
    }
    assert set(by_name["cgm-sanplat-web"]["quality"]["evidence_services"]) == {
        "cgm-artemis-web",
        "cgm-sanplat-web",
    }
    assert set(by_name["cgm-artemis-api"]["quality"]["evidence_services"]) == {
        "cgm-artemis-api",
        "cgm-sanplat-api",
    }


def test_each_service_points_to_its_own_repository():
    services = client.get("/api/catalog/services").json()["services"]
    by_name = {service["service_name"]: service for service in services}
    assert by_name["cgm-sanplat-api"]["repository"] == "diegomad14/cgm-sanplat-api"
    assert by_name["cgm-sanplat-web"]["repository"] == "diegomad14/cgm-sanplat-web"
    assert by_name["cgm-bot-api"]["repository"] == "diegomad14/cgm-bot-core"
    assert by_name["communications-ms"]["quality"]["enabled"] is True
    # Post-split, no service deploys from the archived monorepo.
    assert all(
        service["repository"] != "diegomad14/parametrizacion-correos-cgm"
        for service in services
    )


def test_get_service_found():
    response = client.get("/api/catalog/services/cgm-sanplat-api")
    assert response.status_code == 200
    data = response.json()
    assert data["service_name"] == "cgm-sanplat-api"
    assert "display_name" not in data
    assert data["project_id"] == "cgm-assistant-prod"
    assert data["finops"]["service"] == "cgm-sanplat-api"
    assert "latest_ready_revision" in data
    assert "traffic" in data


def test_get_service_not_found():
    assert client.get("/api/catalog/services/nonexistent").status_code == 404


def test_application_endpoints_are_removed():
    assert client.get("/api/catalog/apps").status_code == 404


def test_job_health_reads_job_runtime_not_service_runtime(monkeypatch):
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(catalog, "_detail_cache", {})
    job = SimpleNamespace(
        terminal_condition=SimpleNamespace(
            state=SimpleNamespace(name="CONDITION_SUCCEEDED")
        ),
        reconciling=False,
        latest_created_execution=SimpleNamespace(
            completion_status=SimpleNamespace(name="EXECUTION_SUCCEEDED")
        ),
        generation=7,
    )
    monkeypatch.setattr(
        catalog, "_job_client", lambda: SimpleNamespace(get_job=lambda **kwargs: job)
    )
    monkeypatch.setattr(
        catalog,
        "_run_client",
        lambda: (_ for _ in ()).throw(AssertionError("Service client used")),
    )
    detail = catalog.get_service_detail("cgm-artemis-wm-sweep-worker")
    assert detail.status == "healthy"
    assert detail.latest_ready_revision == "job-generation-7"
