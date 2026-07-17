"""Contract tests for GitHub-native deployment endpoints."""

import tempfile
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from eng_platform_api.main import app
from eng_platform_api.models import DeploymentItem, ReleaseTag, ReleaseTagPage

tmp_store = Path(tempfile.mkdtemp(prefix="deployments_test_")) / "deployments.json"


@pytest.fixture(autouse=True)
def isolated_store():
    with (
        mock.patch(
            "eng_platform_api.services.deployment_store._DEFAULT_STORE_PATH",
            tmp_store,
        ),
        mock.patch("eng_platform_api.services.deployment_store._COLLECTION", ""),
    ):
        tmp_store.unlink(missing_ok=True)
        yield
        tmp_store.unlink(missing_ok=True)


@pytest.fixture
def client():
    return TestClient(app)


def _tag(eligible=True):
    return ReleaseTag(
        name="v0.5.0",
        sha="a" * 40,
        created_at="2026-07-16T12:00:00+00:00",
        eligible=eligible,
        reason="Already deployed" if not eligible else "",
    )


def _deployment():
    return DeploymentItem(
        id="42",
        service_name="eng-platform-api",
        repository="diegomad14/gcp-engineering-platform-api",
        tag="v0.5.0",
        sha="a" * 40,
        github_deployment_id=42,
        created_at="2026-07-16T12:00:00+00:00",
        updated_at="2026-07-16T12:00:00+00:00",
    )


def test_list_tags_is_service_oriented(client):
    with mock.patch(
        "eng_platform_api.routers.deployments.github_deployments.list_tags",
        return_value=ReleaseTagPage(items=[_tag()], next_cursor="10"),
    ) as list_tags:
        response = client.get("/api/services/eng-platform-api/tags?limit=10")
    assert response.status_code == 200
    assert response.json()["items"][0]["name"] == "v0.5.0"
    assert response.json()["next_cursor"] == "10"
    assert list_tags.call_args.args[:2] == (
        "diegomad14/gcp-engineering-platform-api",
        "eng-platform-api",
    )


def test_unknown_service_does_not_call_github(client):
    response = client.get("/api/services/not-real/tags")
    assert response.status_code == 404


def test_create_deployment_and_idempotent_replay(client):
    with (
        mock.patch(
            "eng_platform_api.routers.deployments.github_deployments.get_tag",
            return_value=_tag(),
        ),
        mock.patch(
            "eng_platform_api.routers.deployments.github_deployments.start_deployment",
            return_value=_deployment(),
        ) as start,
    ):
        headers = {"Idempotency-Key": "deploy-api-v050"}
        first = client.post(
            "/api/services/eng-platform-api/deployments",
            json={"tag": "v0.5.0"},
            headers=headers,
        )
        second = client.post(
            "/api/services/eng-platform-api/deployments",
            json={"tag": "v0.5.0"},
            headers=headers,
        )
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert start.call_count == 1


def test_ineligible_tag_is_rejected(client):
    with mock.patch(
        "eng_platform_api.routers.deployments.github_deployments.get_tag",
        return_value=_tag(eligible=False),
    ):
        response = client.post(
            "/api/services/eng-platform-api/deployments",
            json={"tag": "v0.5.0"},
        )
    assert response.status_code == 409
    assert response.json()["detail"] == "Already deployed"


def test_get_and_list_reconstruct_from_store(client):
    from eng_platform_api.services import deployment_store

    deployment_store.save(_deployment(), "key")
    with mock.patch(
        "eng_platform_api.routers.deployments.github_deployments.refresh",
        side_effect=lambda item: item.model_copy(update={"status": "SUCCEEDED"}),
    ):
        detail = client.get("/api/deployments/42")
        listing = client.get("/api/services/eng-platform-api/deployments")
    assert detail.status_code == 200
    assert detail.json()["status"] == "SUCCEEDED"
    assert listing.json()["items"][0]["id"] == "42"
