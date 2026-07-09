"""Tests for the releases webhook and query endpoints."""
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

# Patch the store path to use a temp file before importing the app module
# to avoid touching the real data/releases.json.
tmp_dir = tempfile.mkdtemp(prefix="releases_test_")
tmp_store = Path(tmp_dir) / "releases.json"


@pytest.fixture(autouse=True)
def _isolated_store():
    with mock.patch(
        "eng_platform_api.services.releases_store._DEFAULT_STORE_PATH", tmp_store
    ):
        # Clear any leftover state
        if tmp_store.exists():
            tmp_store.unlink()
        yield
        if tmp_store.exists():
            tmp_store.unlink()


@pytest.fixture
def client():
    from eng_platform_api.main import app

    return TestClient(app)


def _release_payload(**overrides):
    return {
        "app_id": "cgm-integration-platform",
        "app_name": "CGM Integration Platform",
        "version": "v0.9.6",
        "status": "candidate",
        "services": [
            {"service_name": "cgm-sanplat-api", "revision": "cgm-sanplat-api-00173-5cs", "action": "deployed"},
            {"service_name": "cgm-sanplat-web", "revision": "cgm-sanplat-web-00088-bx5", "action": "deployed"},
        ],
        "github_run_url": "https://github.com/diegomad14/parametrizacion-correos-cgm/actions/runs/123",
        **overrides,
    }


# ── POST /api/releases ──────────────────────────────────────────────────

def test_register_candidate(client):
    resp = client.post("/api/releases", json=_release_payload())
    assert resp.status_code == 201
    data = resp.json()
    assert data["app_id"] == "cgm-integration-platform"
    assert data["version"] == "v0.9.6"
    assert data["status"] == "candidate"
    assert data["api_revision"] == "cgm-sanplat-api-00173-5cs"
    assert data["web_revision"] == "cgm-sanplat-web-00088-bx5"
    assert data["created_at"]


def test_register_promoted(client):
    resp = client.post("/api/releases", json=_release_payload(status="promoted"))
    assert resp.status_code == 201
    assert resp.json()["status"] == "promoted"


def test_register_rollback(client):
    resp = client.post("/api/releases", json=_release_payload(
        status="rolled_back",
        rollback_from_version="v0.9.5",
        services=[
            {"service_name": "cgm-sanplat-api", "revision": "cgm-sanplat-api-00155-abc", "action": "rolled_back"},
        ],
        notes="Emergency rollback due to CORS incident",
    ))
    assert resp.status_code == 201
    assert resp.json()["status"] == "rolled_back"


def test_register_missing_version(client):
    payload = _release_payload()
    del payload["version"]
    resp = client.post("/api/releases", json=payload)
    assert resp.status_code == 422  # FastAPI validation error


# ── GET /api/releases ────────────────────────────────────────────────────

def test_list_releases_empty(client):
    resp = client.get("/api/releases")
    assert resp.status_code == 200
    assert resp.json()["total_releases"] == 0
    assert resp.json()["recent"] == []


def test_list_releases_after_insert(client):
    client.post("/api/releases", json=_release_payload(version="v0.9.5"))
    client.post("/api/releases", json=_release_payload(version="v0.9.6", status="promoted"))

    resp = client.get("/api/releases")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_releases"] == 2
    assert len(body["recent"]) == 2
    assert body["recent"][0]["version"] == "v0.9.6"  # newest first


def test_list_releases_filter_by_app(client):
    client.post("/api/releases", json=_release_payload(app_id="app-a", app_name="App A"))
    client.post("/api/releases", json=_release_payload(app_id="app-b", app_name="App B"))

    resp = client.get("/api/releases?app_id=app-a")
    assert resp.status_code == 200
    assert resp.json()["total_releases"] == 1


def test_list_releases_limit(client):
    for i in range(5):
        client.post("/api/releases", json=_release_payload(version=f"v0.9.{i}"))
    resp = client.get("/api/releases?limit=3")
    assert len(resp.json()["recent"]) == 3


# ── GET /api/releases/{app_id}/latest ────────────────────────────────────

def test_latest_release(client):
    client.post("/api/releases", json=_release_payload(version="v0.9.5"))
    client.post("/api/releases", json=_release_payload(version="v0.9.6", status="promoted"))

    resp = client.get("/api/releases/cgm-integration-platform/latest")
    assert resp.status_code == 200
    assert resp.json()["version"] == "v0.9.6"


def test_latest_release_not_found(client):
    resp = client.get("/api/releases/nonexistent/latest")
    assert resp.status_code == 404


# ── GET /api/releases/summary ────────────────────────────────────────────

def test_summary_includes_stored_releases(client):
    client.post("/api/releases", json=_release_payload(version="v0.9.6", status="promoted"))
    resp = client.get("/api/releases/summary")
    assert resp.status_code == 200
    # Should include our stored release + possibly mock data from github_actions
    assert resp.json()["total_releases"] >= 1


# ── Thread safety ────────────────────────────────────────────────────────

def test_concurrent_writes(client):
    import threading

    errors = []

    def post_release(i):
        try:
            resp = client.post("/api/releases", json=_release_payload(version=f"v0.{i}.0"))
            if resp.status_code != 201:
                errors.append(f"writer {i}: status {resp.status_code}")
        except Exception as e:
            errors.append(f"writer {i}: {e}")

    threads = [threading.Thread(target=post_release, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Concurrent write errors: {errors}"
    resp = client.get("/api/releases")
    assert resp.json()["total_releases"] == 10
