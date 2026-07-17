"""Tests for service-oriented release history."""

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

tmp_store = Path(tempfile.mkdtemp(prefix="releases_test_")) / "releases.json"


@pytest.fixture(autouse=True)
def _isolated_store():
    with mock.patch(
        "eng_platform_api.services.releases_store._DEFAULT_STORE_PATH",
        tmp_store,
    ):
        if tmp_store.exists():
            tmp_store.unlink()
        yield
        if tmp_store.exists():
            tmp_store.unlink()


@pytest.fixture
def client():
    from eng_platform_api.main import app

    return TestClient(app)


def _payload(**overrides):
    return {
        "repository": "diegomad14/parametrizacion-correos-cgm",
        "version": "v0.9.6",
        "status": "candidate",
        "services": [
            {
                "service_name": "cgm-sanplat-api",
                "revision": "cgm-sanplat-api-00173-5cs",
                "action": "deployed",
            },
            {
                "service_name": "cgm-sanplat-web",
                "revision": "cgm-sanplat-web-00088-bx5",
                "action": "deployed",
            },
        ],
        "github_run_url": "https://github.com/diegomad14/parametrizacion-correos-cgm/actions/runs/123",
        **overrides,
    }


def test_register_creates_one_row_per_service(client):
    response = client.post("/api/releases", json=_payload())
    assert response.status_code == 201
    rows = response.json()
    assert len(rows) == 2
    assert {row["service_name"] for row in rows} == {
        "cgm-sanplat-api",
        "cgm-sanplat-web",
    }
    assert all(row["repository"] == _payload()["repository"] for row in rows)
    assert all("services" not in row for row in rows)


def test_api_only_release_is_not_synthesized_into_other_services(client):
    response = client.post(
        "/api/releases",
        json=_payload(services=[_payload()["services"][0]]),
    )
    assert response.status_code == 201
    assert [row["service_name"] for row in response.json()] == ["cgm-sanplat-api"]


def test_incomplete_revision_becomes_missing(client):
    service = {
        "service_name": "cgm-sanplat-api",
        "revision": "",
        "action": "deployed",
    }
    row = client.post("/api/releases", json=_payload(services=[service])).json()[0]
    assert row["action"] == "missing"


def test_release_request_rejects_legacy_application_fields(client):
    payload = _payload(app_id="legacy", app_name="Legacy")
    assert client.post("/api/releases", json=payload).status_code == 422


def test_release_request_rejects_unknown_action(client):
    service = {
        "service_name": "cgm-sanplat-api",
        "revision": "rev",
        "action": "skipped",
    }
    assert (
        client.post("/api/releases", json=_payload(services=[service])).status_code
        == 422
    )


def test_list_and_filter_releases_by_service(client):
    client.post("/api/releases", json=_payload())
    response = client.get("/api/releases?service_name=cgm-sanplat-api")
    assert response.status_code == 200
    assert response.json()["total_releases"] == 1
    assert response.json()["recent"][0]["service_name"] == "cgm-sanplat-api"


def test_release_counter_counts_service_rows(client):
    client.post("/api/releases", json=_payload())
    response = client.get("/api/releases")
    assert response.json()["total_releases"] == 2
    assert len(response.json()["recent"]) == 2


def test_latest_release_is_per_service(client):
    client.post("/api/releases", json=_payload(version="v0.9.5"))
    client.post("/api/releases", json=_payload(version="v0.9.6", status="promoted"))
    response = client.get("/api/releases/cgm-sanplat-web/latest")
    assert response.status_code == 200
    assert response.json()["version"] == "v0.9.6"


def test_latest_release_not_found(client):
    assert client.get("/api/releases/nonexistent/latest").status_code == 404


def test_grouped_historical_record_is_split_on_read(client):
    tmp_store.write_text(
        json.dumps(
            [
                {
                    "app_id": "cgm-integration-platform",
                    "version": "v0.9.10",
                    "status": "promoted",
                    "services": [
                        {
                            "service_name": "cgm-sanplat-api",
                            "revision": "cgm-sanplat-api-00012-4mz",
                            "action": "promoted",
                        },
                        {
                            "service_name": "cgm-sanplat-web",
                            "revision": "",
                            "action": "not_included",
                        },
                    ],
                    "created_at": "2026-07-10T12:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    rows = client.get("/api/releases").json()["recent"]
    assert len(rows) == 2
    assert {row["service_name"] for row in rows} == {
        "cgm-sanplat-api",
        "cgm-sanplat-web",
    }


def test_legacy_fixed_revision_record_is_recovered(client):
    tmp_store.write_text(
        json.dumps(
            [
                {
                    "app_id": "cgm-integration-platform",
                    "version": "v0.9.10",
                    "status": "promoted",
                    "api_revision": "cgm-sanplat-api-00012-4mz",
                    "web_revision": "",
                    "created_at": "2026-07-10T12:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    rows = client.get("/api/releases").json()["recent"]
    assert len(rows) == 1
    assert rows[0]["service_name"] == "cgm-sanplat-api"


def test_unidentifiable_historical_record_is_discarded(client):
    tmp_store.write_text(
        json.dumps(
            [
                {
                    "app_id": "unknown",
                    "version": "legacy",
                    "services": [
                        {"service_name": "unknown", "revision": "", "action": "missing"}
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    assert client.get("/api/releases").json()["total_releases"] == 0


def test_summary_deduplicates_same_run_per_service(client):
    from eng_platform_api.models import ReleaseItem, ReleaseSummary

    stored = client.post(
        "/api/releases", json=_payload(services=[_payload()["services"][0]])
    ).json()[0]
    discovered = ReleaseItem(**stored)
    with mock.patch(
        "eng_platform_api.routers.releases.github_actions.get_release_summary",
        return_value=ReleaseSummary(recent=[discovered], total_releases=1),
    ):
        response = client.get("/api/releases/summary")
    assert response.json()["total_releases"] == 1


def test_concurrent_writes_preserve_all_service_rows(client):
    def register(index: int):
        return client.post(
            "/api/releases",
            json=_payload(
                version=f"v0.0.{index}", services=[_payload()["services"][0]]
            ),
        ).status_code

    with ThreadPoolExecutor(max_workers=5) as executor:
        assert set(executor.map(register, range(10))) == {201}
    assert client.get("/api/releases?limit=20").json()["total_releases"] == 10


def test_firestore_save_and_query_uses_collection_backend():
    """Firestore backend saves and queries per-service release rows."""
    from unittest import mock as um

    from eng_platform_api.models import ReleaseCreateRequest, ServiceRevision
    from eng_platform_api.services import releases_store

    doc_mock = um.MagicMock()
    collection_mock = um.MagicMock()
    collection_mock.document.return_value = doc_mock
    doc_mock.get.return_value = um.MagicMock(exists=False, to_dict=lambda: {})

    with (
        um.patch.object(
            releases_store,
            "_firestore_collection",
            return_value=collection_mock,
        ),
        um.patch.object(
            releases_store,
            "_COLLECTION",
            "test_releases",
        ),
    ):
        releases_store.save_release(
            ReleaseCreateRequest(
                repository="diegomad14/test-repo",
                version="v1.0.0",
                services=[
                    ServiceRevision(
                        service_name="test-api",
                        revision="test-api-00001-abc",
                        action="promoted",
                    )
                ],
            )
        )
        doc_mock.set.assert_called_once()
        args = doc_mock.set.call_args[0][0]
        assert args["service_name"] == "test-api"
        assert args["repository"] == "diegomad14/test-repo"
        assert args["version"] == "v1.0.0"
        assert args["action"] == "promoted"

        # Also exercise get_releases and count_releases via Firestore
        saved_record = {
            "service_name": "test-api",
            "repository": "diegomad14/test-repo",
            "version": "v1.0.0",
            "status": "promoted",
            "revision": "test-api-00001-abc",
            "action": "promoted",
            "github_run_url": "",
            "created_at": "2026-07-17T00:00:00Z",
        }
        mock_stream = [um.MagicMock(to_dict=lambda r=saved_record: r)]
        collection_mock.order_by.return_value.limit.return_value.stream.return_value = (
            mock_stream
        )
        collection_mock.where.return_value.order_by.return_value.limit.return_value.stream.return_value = mock_stream
        collection_mock.where.return_value.stream.return_value = mock_stream

        results = releases_store.get_releases(service_name="test-api", limit=5)
        assert len(results) == 1
        assert results[0].service_name == "test-api"
        assert results[0].action == "promoted"

        count = releases_store.count_releases(service_name="test-api")
        assert count == 1


def test_firestore_collection_returns_client_when_configured():
    """_firestore_collection creates a Firestore client when _COLLECTION is set."""
    from unittest import mock as um

    from eng_platform_api.services import releases_store

    fake_client = um.MagicMock()
    fake_collection = um.MagicMock()
    fake_client.collection.return_value = fake_collection

    with (
        um.patch.object(releases_store, "_COLLECTION", "test_releases"),
        um.patch("os.getenv", return_value="test-project"),
        um.patch("google.cloud.firestore.Client") as mock_client_cls,
    ):
        mock_client_cls.return_value = fake_client
        result = releases_store._firestore_collection()
        assert result is fake_collection
        fake_client.collection.assert_called_once_with("test_releases")

    # When _COLLECTION is empty, returns None
    with um.patch.object(releases_store, "_COLLECTION", ""):
        assert releases_store._firestore_collection() is None
