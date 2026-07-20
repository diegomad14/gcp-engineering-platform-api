"""Tests for the flat service catalog."""

from fastapi.testclient import TestClient

from eng_platform_api.main import app

client = TestClient(app)


def test_list_services():
    response = client.get("/api/catalog/services")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 6
    assert len(data["services"]) == 6
    assert all("display_name" not in service for service in data["services"])
    names = {service["service_name"] for service in data["services"]}
    assert {"cgm-sanplat-api", "cgm-sanplat-web", "eng-platform-api"} <= names


def test_each_service_points_to_its_own_repository():
    services = client.get("/api/catalog/services").json()["services"]
    by_name = {service["service_name"]: service for service in services}
    assert by_name["cgm-sanplat-api"]["repository"] == "diegomad14/cgm-sanplat-api"
    assert by_name["cgm-sanplat-web"]["repository"] == "diegomad14/cgm-sanplat-web"
    assert by_name["cgm-bot-api"]["repository"] == "diegomad14/cgm-bot-core"
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
