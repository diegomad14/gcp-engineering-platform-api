"""Contract tests for GitHub-native deployment endpoints."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
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
        mock.patch(
            "eng_platform_api.routers.deployments.require_deployer",
            return_value="diegomad14",
        ),
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


def _succeeded_deployment(
    tag="v0.4.0",
    production_revision="eng-platform-api-00010-abc",
    created_at="2026-07-10T12:00:00+00:00",
    status="SUCCEEDED",
):
    return DeploymentItem(
        id=f"id-{tag}",
        service_name="eng-platform-api",
        repository="diegomad14/gcp-engineering-platform-api",
        tag=tag,
        sha="b" * 40,
        status=status,
        production_revision=production_revision,
        created_at=created_at,
        updated_at=created_at,
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


def test_idempotency_key_cannot_be_reused_for_another_tag(client):
    from eng_platform_api.services import deployment_store

    deployment_store.save(_deployment(), "one-request")
    response = client.post(
        "/api/services/eng-platform-api/deployments",
        json={"tag": "v0.5.1"},
        headers={"Idempotency-Key": "one-request"},
    )
    assert response.status_code == 409
    assert "another deployment" in response.json()["detail"]


def test_second_active_deployment_is_rejected(client):
    from eng_platform_api.services import deployment_store

    deployment_store.save(_deployment(), "first-request")
    response = client.post(
        "/api/services/eng-platform-api/deployments",
        json={"tag": "v0.5.1"},
        headers={"Idempotency-Key": "second-request"},
    )
    assert response.status_code == 409
    assert "already active" in response.json()["detail"]


def test_negative_tag_cursor_is_rejected(client):
    response = client.get("/api/services/eng-platform-api/tags?cursor=-1")
    assert response.status_code == 400


def test_non_numeric_tag_cursor_is_rejected(client):
    response = client.get("/api/services/eng-platform-api/tags?cursor=not-a-number")
    assert response.status_code == 400


def test_operational_value_error_is_not_reported_as_invalid_cursor(client):
    with mock.patch(
        "eng_platform_api.routers.deployments.github_deployments.list_tags",
        side_effect=ValueError("Project ID is required"),
    ):
        response = client.get("/api/services/eng-platform-api/tags")
    assert response.status_code == 502
    assert response.json()["detail"] == "GitHub unavailable"


def test_github_token_is_trimmed(monkeypatch):
    from eng_platform_api.config import load_config

    monkeypatch.setenv("ENG_PLATFORM_GITHUB_TOKEN", "token-with-whitespace\n")
    assert load_config().github.token == "token-with-whitespace"


def test_create_deployment_hides_upstream_error_details(client):
    with mock.patch(
        "eng_platform_api.routers.deployments.github_deployments.get_tag",
        side_effect=RuntimeError("sensitive provider detail"),
    ):
        response = client.post(
            "/api/services/eng-platform-api/deployments",
            json={"tag": "v0.6.3"},
        )
    assert response.status_code == 502
    assert response.json()["detail"] == "GitHub unavailable"


def test_deployment_reads_hide_upstream_error_details(client):
    from eng_platform_api.services import deployment_store

    deployment_store.save(_deployment(), "key")
    with mock.patch(
        "eng_platform_api.routers.deployments.github_deployments.refresh",
        side_effect=RuntimeError("sensitive provider detail"),
    ):
        listing = client.get("/api/services/eng-platform-api/deployments")
        detail = client.get("/api/deployments/42")
    assert listing.json()["items"][0]["error"] == "GitHub unavailable"
    assert detail.json()["error"] == "GitHub unavailable"


def test_refresh_hides_upstream_error_details():
    from eng_platform_api.services import github_deployments

    github = mock.MagicMock()
    github.get_repo.return_value.get_deployment.side_effect = RuntimeError(
        "sensitive provider detail"
    )
    with (
        mock.patch.object(github_deployments.config, "mock_mode", False),
        mock.patch.object(github_deployments, "github_client", return_value=github),
    ):
        item = github_deployments.refresh(_deployment())
    assert item.error == "Unable to read GitHub workflow"


def test_firestore_client_uses_configured_project(monkeypatch):
    from eng_platform_api.services import deployment_store

    firestore_client = mock.MagicMock()
    monkeypatch.setenv("ENG_PLATFORM_GCP_PROJECT_ID", "cgm-assistant-prod")
    with (
        mock.patch.object(deployment_store, "_COLLECTION", "eng_platform_deployments"),
        mock.patch(
            "google.cloud.firestore.Client", return_value=firestore_client
        ) as client_factory,
    ):
        collection = deployment_store._firestore_collection()
    client_factory.assert_called_once_with(project="cgm-assistant-prod")
    assert collection is firestore_client.collection.return_value


def test_firestore_client_can_use_default_project_discovery(monkeypatch):
    from eng_platform_api.services import deployment_store

    firestore_client = mock.MagicMock()
    monkeypatch.delenv("ENG_PLATFORM_GCP_PROJECT_ID", raising=False)
    with (
        mock.patch.object(deployment_store, "_COLLECTION", "deployments"),
        mock.patch(
            "google.cloud.firestore.Client", return_value=firestore_client
        ) as client_factory,
    ):
        collection = deployment_store._firestore_collection()
    client_factory.assert_called_once_with()
    assert collection is firestore_client.collection.return_value


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
    assert listing.json()["total"] == 1


def test_dispatch_uses_independent_service_catalog_configuration():
    from eng_platform_api.services import catalog, github_deployments

    service = catalog.get_service("cgm-sanplat-web")
    assert service is not None
    repository = mock.MagicMock()
    github_deployment = mock.MagicMock(id=73)
    repository.create_deployment.return_value = github_deployment
    workflow = repository.get_workflow.return_value
    github = mock.MagicMock()
    github.get_repo.return_value = repository

    with (
        mock.patch.object(github_deployments.config, "mock_mode", False),
        mock.patch.object(github_deployments, "github_client", return_value=github),
    ):
        item = github_deployments.start_deployment(
            service=service,
            tag=_tag(),
            requested_by="diegomad14",
        )

    assert item.id == "73"
    repository.get_workflow.assert_called_once_with("platform-deploy.yml")
    inputs = workflow.create_dispatch.call_args.kwargs["inputs"]
    assert inputs["service_name"] == "cgm-sanplat-web"
    assert inputs["build_context"] == "frontend"
    assert inputs["health_path"] == "/"
    assert inputs["project_id"] == "cgm-assistant-prod"


def test_rollback_target_must_exist(client):
    response = client.post(
        "/api/services/eng-platform-api/deployments/missing/rollback",
        headers={"Idempotency-Key": "rb-missing"},
    )
    assert response.status_code == 404


def test_rollback_target_must_be_succeeded(client):
    from eng_platform_api.services import deployment_store

    target = _succeeded_deployment(status="FAILED")
    deployment_store.save(target, "")
    response = client.post(
        f"/api/services/eng-platform-api/deployments/{target.id}/rollback",
        headers={"Idempotency-Key": "rb-failed"},
    )
    assert response.status_code == 409


def test_rollback_dispatches_and_persists(client):
    from eng_platform_api.services import deployment_store

    target = _succeeded_deployment()
    deployment_store.save(target, "")
    rollback_item = DeploymentItem(
        id="99",
        service_name="eng-platform-api",
        repository="diegomad14/gcp-engineering-platform-api",
        tag=target.tag,
        sha=target.sha,
        kind="rollback",
        created_at="2026-07-17T12:00:00+00:00",
        updated_at="2026-07-17T12:00:00+00:00",
        github_deployment_id=99,
    )
    with mock.patch(
        "eng_platform_api.routers.deployments.github_deployments.start_rollback",
        return_value=rollback_item,
    ) as start:
        response = client.post(
            f"/api/services/eng-platform-api/deployments/{target.id}/rollback",
            headers={"Idempotency-Key": "rb-ok"},
        )
    assert response.status_code == 202
    assert response.json()["kind"] == "rollback"
    assert start.call_args.kwargs["target"].id == target.id


def test_rollback_idempotent_replay(client):
    from eng_platform_api.services import deployment_store

    target = _succeeded_deployment()
    deployment_store.save(target, "")
    rollback_item = DeploymentItem(
        id="100",
        service_name="eng-platform-api",
        repository="diegomad14/gcp-engineering-platform-api",
        tag=target.tag,
        kind="rollback",
        created_at="2026-07-17T12:00:00+00:00",
        updated_at="2026-07-17T12:00:00+00:00",
    )
    with mock.patch(
        "eng_platform_api.routers.deployments.github_deployments.start_rollback",
        return_value=rollback_item,
    ) as start:
        headers = {"Idempotency-Key": "rb-replay"}
        first = client.post(
            f"/api/services/eng-platform-api/deployments/{target.id}/rollback",
            headers=headers,
        )
        second = client.post(
            f"/api/services/eng-platform-api/deployments/{target.id}/rollback",
            headers=headers,
        )
    assert first.json()["id"] == second.json()["id"]
    assert start.call_count == 1


def test_rollback_rejected_when_active_deployment_exists(client):
    from eng_platform_api.services import deployment_store

    target = _succeeded_deployment()
    deployment_store.save(target, "")
    deployment_store.save(_deployment(), "active-deploy")  # defaults to QUEUED
    response = client.post(
        f"/api/services/eng-platform-api/deployments/{target.id}/rollback",
        headers={"Idempotency-Key": "rb-blocked"},
    )
    assert response.status_code == 409
    assert "already active" in response.json()["detail"]


def test_list_tags_eligibility_follows_current_live_tag():
    from eng_platform_api.services import deployment_store, github_deployments

    superseded = _succeeded_deployment(
        tag="v0.4.0", created_at="2026-07-01T12:00:00+00:00"
    )
    live = _succeeded_deployment(
        tag="v0.5.0", created_at="2026-07-16T12:00:00+00:00"
    )
    deployment_store.save(superseded, "")
    deployment_store.save(live, "")

    tags = [
        SimpleNamespace(name="v0.5.0", commit=SimpleNamespace(sha="c" * 40)),
        SimpleNamespace(name="v0.4.0", commit=SimpleNamespace(sha="d" * 40)),
    ]
    repository = mock.MagicMock()
    repository.get_tags.return_value = tags
    repository.get_commit.side_effect = RuntimeError("no commit metadata in test")
    github = mock.MagicMock()
    github.get_repo.return_value = repository

    with (
        mock.patch.object(github_deployments.config, "mock_mode", False),
        mock.patch.object(github_deployments, "github_client", return_value=github),
    ):
        page = github_deployments.list_tags(
            "diegomad14/gcp-engineering-platform-api", "eng-platform-api", limit=10
        )

    by_name = {tag.name: tag for tag in page.items}
    assert by_name["v0.5.0"].eligible is False
    assert by_name["v0.5.0"].reason == "This tag is already live in production"
    assert by_name["v0.4.0"].eligible is True


def test_deployment_statuses_supply_run_and_revision_evidence():
    from eng_platform_api.services import github_deployments

    item = _deployment()
    statuses = [
        SimpleNamespace(
            log_url="https://github.com/diegomad14/repo/actions/runs/314",
            target_url="",
            description="candidate_revision=service-00012-candidate",
            environment_url="https://candidate.example",
        ),
        SimpleNamespace(
            log_url="",
            target_url="",
            description="production_revision=service-00012-production",
            environment_url="https://production.example",
        ),
    ]
    repository = mock.MagicMock()
    repository.get_deployment.return_value.get_statuses.return_value = statuses

    run_id = github_deployments._metadata_from_statuses(repository, item)

    assert run_id == 314
    assert item.candidate_revision == "service-00012-candidate"
    assert item.candidate_url == "https://candidate.example"
    assert item.production_revision == "service-00012-production"
    assert item.production_url == "https://production.example"
