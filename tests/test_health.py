"""Tests for health check endpoint."""

import json

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


def test_ready_validates_authoritative_catalog_without_disclosing_contents(monkeypatch):
    loader = health.log_catalog.load_catalog
    monkeypatch.setattr(
        health.log_catalog,
        "load_catalog",
        lambda: loader(health.log_catalog.CATALOG_PATH),
    )
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_ready_rejects_conflicting_repository_defaults_but_liveness_survives(
    tmp_path, monkeypatch
):
    source = json.loads(health.log_catalog.CATALOG_PATH.read_text())
    entries = source["services"][:2]
    entries[1]["repository"] = entries[0]["repository"]
    entries[0]["release"] = {"executor": "github_actions"}
    entries[1]["release"] = {"executor": "cloud_build"}
    path = tmp_path / "PRIVATE-invalid-catalog.json"
    path.write_text(json.dumps({"services": entries}))
    loader = health.log_catalog.load_catalog
    monkeypatch.setattr(health.log_catalog, "load_catalog", lambda: loader(path))
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json() == {"detail": "Platform is not ready"}
    assert "PRIVATE" not in response.text
    assert client.get("/health").status_code == 200
