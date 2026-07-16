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


def test_services_sharing_repository_remain_independent():
    services = client.get("/api/catalog/services").json()["services"]
    cgm = [
        service
        for service in services
        if service["repository"] == "diegomad14/parametrizacion-correos-cgm"
    ]
    assert {service["service_name"] for service in cgm} == {
        "cgm-sanplat-api",
        "cgm-sanplat-web",
    }
    bot = next(
        service for service in services if service["service_name"] == "cgm-bot-api"
    )
    assert bot["repository"] == "diegomad14/cgm-bot-core"


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
