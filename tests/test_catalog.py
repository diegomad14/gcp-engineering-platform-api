"""Tests for catalog endpoints."""

from fastapi.testclient import TestClient

from eng_platform_api.main import app

client = TestClient(app)


def test_list_applications():
    response = client.get("/api/catalog/apps")
    assert response.status_code == 200
    data = response.json()
    assert "applications" in data
    assert len(data["applications"]) >= 1
    assert data["applications"][0]["id"] == "cgm-integration-platform"


def test_get_application_found():
    response = client.get("/api/catalog/apps/cgm-integration-platform")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == "cgm-integration-platform"
    assert len(data["release_targets"]) >= 1


def test_get_application_not_found():
    response = client.get("/api/catalog/apps/nonexistent")
    assert response.status_code == 404
