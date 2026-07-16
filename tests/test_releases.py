"""Tests for the releases webhook and query endpoints."""
import json
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
    from eng_platform_api.models import Application, CloudRunService
    from eng_platform_api.services import releases_store

    applications = {
        "cgm-integration-platform": Application(
            id="cgm-integration-platform",
            name="CGM Integration Platform",
            repository="diegomad14/parametrizacion-correos-cgm",
            owner="cgm",
            release_targets=[
                CloudRunService(
                    service_name="cgm-sanplat-api",
                    project_id="test-project",
                    region="us-central1",
                ),
                CloudRunService(
                    service_name="cgm-sanplat-web",
                    project_id="test-project",
                    region="us-central1",
                ),
                CloudRunService(
                    service_name="cgm-bot-api",
                    project_id="test-project",
                    region="us-central1",
                ),
            ],
        ),
        "engineering-platform": Application(
            id="engineering-platform",
            name="Engineering Platform",
            repository="diegomad14/gcp-engineering-platform",
            owner="platform",
            release_targets=[
                CloudRunService(
                    service_name="eng-platform-api",
                    project_id="test-project",
                    region="us-central1",
                ),
                CloudRunService(
                    service_name="eng-platform-web",
                    project_id="test-project",
                    region="us-central1",
                ),
            ],
        ),
    }

    with (
        mock.patch(
            "eng_platform_api.services.releases_store._DEFAULT_STORE_PATH",
            tmp_store,
        ),
        mock.patch(
            "eng_platform_api.services.catalog.get_application",
            side_effect=lambda app_id: applications.get(app_id),
        ),
    ):
        releases_store._catalog_service_names.cache_clear()
        # Clear any leftover state
        if tmp_store.exists():
            tmp_store.unlink()
        yield
        releases_store._catalog_service_names.cache_clear()
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


def _services_by_name(data):
    return {service["service_name"]: service for service in data["services"]}


# ── POST /api/releases ──────────────────────────────────────────────────

def test_register_candidate(client):
    resp = client.post("/api/releases", json=_release_payload())
    assert resp.status_code == 201
    data = resp.json()
    assert data["app_id"] == "cgm-integration-platform"
    assert data["version"] == "v0.9.6"
    assert data["status"] == "candidate"
    assert "api_revision" not in data
    assert "web_revision" not in data
    services = _services_by_name(data)
    assert services["cgm-sanplat-api"] == {
        "service_name": "cgm-sanplat-api",
        "revision": "cgm-sanplat-api-00173-5cs",
        "action": "deployed",
    }
    assert services["cgm-sanplat-web"]["revision"] == "cgm-sanplat-web-00088-bx5"
    assert services["cgm-bot-api"]["action"] == "not_included"
    assert data["created_at"]


def test_register_api_only_marks_other_catalog_services_not_included(client):
    resp = client.post(
        "/api/releases",
        json=_release_payload(
            services=[
                {
                    "service_name": "cgm-sanplat-api",
                    "revision": "cgm-sanplat-api-00173-5cs",
                    "action": "deployed",
                }
            ],
        ),
    )

    assert resp.status_code == 201
    services = _services_by_name(resp.json())
    assert services["cgm-sanplat-api"]["action"] == "deployed"
    assert services["cgm-sanplat-web"]["action"] == "not_included"
    assert services["cgm-bot-api"]["action"] == "not_included"


def test_register_web_only_marks_api_not_included(client):
    resp = client.post(
        "/api/releases",
        json=_release_payload(
            services=[
                {
                    "service_name": "cgm-sanplat-web",
                    "revision": "cgm-sanplat-web-00088-bx5",
                    "action": "deployed",
                }
            ],
        ),
    )

    services = _services_by_name(resp.json())
    assert services["cgm-sanplat-api"]["action"] == "not_included"
    assert services["cgm-sanplat-web"]["action"] == "deployed"


def test_register_preserves_explicit_unchanged(client):
    resp = client.post(
        "/api/releases",
        json=_release_payload(
            services=[
                {
                    "service_name": "cgm-sanplat-api",
                    "revision": "cgm-sanplat-api-00173-5cs",
                    "action": "promoted",
                },
                {
                    "service_name": "cgm-sanplat-web",
                    "revision": "cgm-sanplat-web-00088-bx5",
                    "action": "unchanged",
                },
            ],
        ),
    )

    services = _services_by_name(resp.json())
    assert services["cgm-sanplat-api"]["action"] == "promoted"
    assert services["cgm-sanplat-web"]["action"] == "unchanged"


def test_register_incomplete_service_entry_is_missing(client):
    resp = client.post(
        "/api/releases",
        json=_release_payload(
            services=[
                {
                    "service_name": "cgm-sanplat-api",
                    "revision": "",
                    "action": "deployed",
                }
            ],
        ),
    )

    services = _services_by_name(resp.json())
    assert services["cgm-sanplat-api"]["action"] == "missing"
    assert services["cgm-sanplat-web"]["action"] == "not_included"


def test_register_without_services_marks_catalog_services_missing(client):
    resp = client.post("/api/releases", json=_release_payload(services=[]))

    assert resp.status_code == 201
    services = _services_by_name(resp.json())
    assert set(services) == {
        "cgm-sanplat-api",
        "cgm-sanplat-web",
        "cgm-bot-api",
    }
    assert {service["action"] for service in services.values()} == {"missing"}


def test_register_retains_service_not_yet_in_catalog(client):
    resp = client.post(
        "/api/releases",
        json=_release_payload(
            app_id="new-application",
            services=[
                {
                    "service_name": "worker-service",
                    "revision": "worker-service-00001-abc",
                    "action": "deployed",
                }
            ],
        ),
    )

    assert resp.json()["services"] == [
        {
            "service_name": "worker-service",
            "revision": "worker-service-00001-abc",
            "action": "deployed",
        }
    ]


def test_register_rejects_unknown_service_action(client):
    resp = client.post(
        "/api/releases",
        json=_release_payload(
            services=[
                {
                    "service_name": "cgm-sanplat-api",
                    "revision": "cgm-sanplat-api-00173-5cs",
                    "action": "skipped",
                }
            ],
        ),
    )

    assert resp.status_code == 422


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


def test_list_releases_reads_legacy_fixed_revision_records(client):
    tmp_store.write_text(
        json.dumps(
            [
                {
                    "app_id": "cgm-integration-platform",
                    "app_name": "CGM Integration Platform",
                    "version": "v0.9.10",
                    "status": "promoted",
                    "api_revision": "cgm-sanplat-api-00012-4mz",
                    "web_revision": "",
                    "github_run_url": "",
                    "created_at": "2026-07-10T12:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )

    resp = client.get("/api/releases")

    assert resp.status_code == 200
    release = resp.json()["recent"][0]
    services = _services_by_name(release)
    assert services["cgm-sanplat-api"]["revision"] == "cgm-sanplat-api-00012-4mz"
    assert services["cgm-sanplat-api"]["action"] == "promoted"
    assert services["cgm-sanplat-web"]["action"] == "missing"
    assert services["cgm-bot-api"]["action"] == "missing"


def test_list_releases_coerces_unknown_stored_action_to_missing(client):
    tmp_store.write_text(
        json.dumps(
            [
                {
                    "app_id": "communications-ms",
                    "app_name": "Communications Microservice",
                    "version": "legacy-manual",
                    "status": "promoted",
                    "services": [
                        {
                            "service_name": "communications-ms",
                            "revision": "",
                            "action": "skipped",
                        }
                    ],
                    "created_at": "2026-07-10T12:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )

    resp = client.get("/api/releases")

    assert resp.status_code == 200
    assert resp.json()["recent"][0]["services"][0]["action"] == "missing"


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
    from eng_platform_api.models import ReleaseItem, ReleaseSummary

    stored = client.post(
        "/api/releases",
        json=_release_payload(version="v0.9.6", status="promoted"),
    ).json()
    github_release = ReleaseItem(
        app_id="engineering-platform",
        app_name="Engineering Platform",
        version="main",
        status="completed",
        services=[],
        github_run_url="https://github.com/diegomad14/gcp-engineering-platform/actions/runs/456",
        created_at="2026-07-16T18:00:00Z",
    )

    with mock.patch(
        "eng_platform_api.routers.releases.github_actions.get_release_summary",
        return_value=ReleaseSummary(recent=[github_release], total_releases=1),
    ):
        resp = client.get("/api/releases/summary")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["recent"]) == 2
    assert body["total_releases"] == 2
    assert stored["github_run_url"] in {
        release["github_run_url"] for release in body["recent"]
    }


def test_summary_deduplicates_same_github_run(client):
    from eng_platform_api.models import ReleaseItem, ReleaseSummary

    payload = _release_payload()
    client.post("/api/releases", json=payload)
    duplicate = ReleaseItem(
        app_id=payload["app_id"],
        app_name=payload["app_name"],
        version=payload["version"],
        status="completed",
        services=[],
        github_run_url=payload["github_run_url"],
        created_at="2026-07-16T18:00:00Z",
    )

    with mock.patch(
        "eng_platform_api.routers.releases.github_actions.get_release_summary",
        return_value=ReleaseSummary(recent=[duplicate], total_releases=1),
    ):
        resp = client.get("/api/releases/summary")

    assert len(resp.json()["recent"]) == 1
    assert resp.json()["total_releases"] == 1


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
