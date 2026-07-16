"""Tests for health check endpoint."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from eng_platform_api.main import app
from eng_platform_api.routers import health
from eng_platform_api.services.catalog import _is_ready

client = TestClient(app)


def test_health_ok():
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == "0.4.1"


def test_root():
    response = client.get("/")
    assert response.status_code == 200
    data = response.json()
    assert data["service"] == "Engineering Platform API"


def test_services_health_mock_mode(monkeypatch):
    monkeypatch.setattr(health.config, "mock_mode", True)
    response = client.get("/api/health/services")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["services"]
    first = data["services"][0]
    assert first["service_name"]
    assert first["project_id"]
    assert first["region"]
    assert first["status"] == "healthy"
    assert first["checked_at"]


def test_cloud_run_terminal_condition_ready():
    service = SimpleNamespace(
        terminal_condition=SimpleNamespace(
            state=SimpleNamespace(name="CONDITION_SUCCEEDED")
        )
    )
    assert _is_ready(service)
