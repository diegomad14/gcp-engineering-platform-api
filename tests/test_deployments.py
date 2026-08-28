"""Contract tests for GitHub-native deployment endpoints."""

import tempfile
from datetime import datetime
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


def test_create_deployment_persists_failed_dispatch_for_idempotent_recovery(client):
    from eng_platform_api.services import deployment_store, github_deployments

    failed = _deployment().model_copy(
        update={
            "id": "73",
            "github_deployment_id": 73,
            "status": "FAILED",
            "current_stage": "dispatch",
            "error": "GitHub workflow dispatch failed",
        }
    )
    with (
        mock.patch(
            "eng_platform_api.routers.deployments.github_deployments.get_tag",
            return_value=_tag(),
        ),
        mock.patch(
            "eng_platform_api.routers.deployments.github_deployments.start_deployment",
            side_effect=github_deployments.GitHubDispatchError(failed),
        ),
    ):
        response = client.post(
            "/api/services/eng-platform-api/deployments",
            json={"tag": "v0.5.0"},
            headers={"Idempotency-Key": "dispatch-failure"},
        )

    assert response.status_code == 502
    assert response.json()["detail"] == "GitHub workflow dispatch failed"
    saved = deployment_store.get("73")
    assert saved is not None
    assert saved.status == "FAILED"
    assert saved.github_deployment_id == 73
    assert saved.current_stage == "dispatch"


def test_create_deployment_retries_failed_dispatch_with_same_deployment(client):
    from eng_platform_api.services import deployment_store

    failed = _deployment().model_copy(
        update={
            "id": "73",
            "github_deployment_id": 73,
            "status": "FAILED",
            "current_stage": "dispatch",
            "error": "GitHub workflow dispatch failed",
        }
    )
    deployment_store.save(failed, "dispatch-failure")
    retried = failed.model_copy(
        update={"status": "QUEUED", "current_stage": "queued", "error": ""}
    )

    with mock.patch(
        "eng_platform_api.routers.deployments.github_deployments.retry_dispatch",
        return_value=retried,
    ) as retry:
        response = client.post(
            "/api/services/eng-platform-api/deployments",
            json={"tag": "v0.5.0"},
            headers={"Idempotency-Key": "dispatch-failure"},
        )

    assert response.status_code == 202
    assert response.json()["id"] == "73"
    assert response.json()["github_deployment_id"] == 73
    assert response.json()["sha"] == "a" * 40
    assert response.json()["status"] == "QUEUED"
    retry.assert_called_once()


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


def test_terminal_deployments_do_not_call_github(client):
    from eng_platform_api.services import deployment_store

    terminal = _succeeded_deployment()
    deployment_store.save(terminal, "")
    with mock.patch(
        "eng_platform_api.routers.deployments.github_deployments.refresh"
    ) as refresh:
        listing = client.get("/api/services/eng-platform-api/deployments")
        detail = client.get(f"/api/deployments/{terminal.id}")

    assert listing.status_code == 200
    assert detail.status_code == 200
    refresh.assert_not_called()


def test_latest_deployments_use_one_grouped_firestore_query():
    from eng_platform_api.services import deployment_store

    first = _succeeded_deployment()
    second = first.model_copy(
        update={"id": "other", "service_name": "eng-platform-web"}
    )
    collection = mock.MagicMock()
    collection.where.return_value.stream.return_value = [
        mock.MagicMock(to_dict=mock.MagicMock(return_value=first.model_dump())),
        mock.MagicMock(to_dict=mock.MagicMock(return_value=second.model_dump())),
    ]
    with mock.patch.object(
        deployment_store, "_firestore_collection", return_value=collection
    ):
        latest = deployment_store.latest_for_services(
            ["eng-platform-api", "eng-platform-web"]
        )

    collection.where.assert_called_once_with(
        "service_name", "in", ["eng-platform-api", "eng-platform-web"]
    )
    assert set(latest) == {"eng-platform-api", "eng-platform-web"}


def test_deployment_overview_aggregates_services(client):
    from eng_platform_api.routers import deployments
    from eng_platform_api.services import catalog, deployment_store

    deployments._overview_cache = None
    deployment_store.save(_succeeded_deployment(), "")
    with mock.patch.object(
        catalog,
        "get_service_detail",
        return_value=SimpleNamespace(
            status="healthy",
            latest_ready_revision="eng-platform-api-00010-abc",
        ),
    ):
        response = client.get("/api/deployments/overview")

    assert response.status_code == 200
    item = next(
        row
        for row in response.json()["items"]
        if row["service_name"] == "eng-platform-api"
    )
    assert item["status"] == "healthy"
    assert item["last_deployment"]["status"] == "SUCCEEDED"


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
    assert inputs["build_context"] == "."
    assert inputs["health_path"] == "/"
    assert inputs["project_id"] == "cgm-assistant-prod"


def test_dispatch_failure_marks_github_deployment_failed():
    from eng_platform_api.services import catalog, github_deployments

    service = catalog.get_service("cgm-sanplat-web")
    assert service is not None
    repository = mock.MagicMock()
    github_deployment = mock.MagicMock(id=74)
    repository.create_deployment.return_value = github_deployment
    repository.get_workflow.return_value.create_dispatch.side_effect = RuntimeError(
        "payment blocked"
    )
    github = mock.MagicMock()
    github.get_repo.return_value = repository

    with (
        mock.patch.object(github_deployments.config, "mock_mode", False),
        mock.patch.object(github_deployments, "github_client", return_value=github),
        pytest.raises(github_deployments.GitHubDispatchError) as raised,
    ):
        github_deployments.start_deployment(
            service=service,
            tag=_tag(),
            requested_by="diegomad14",
        )

    assert raised.value.item.id == "74"
    assert raised.value.item.status == "FAILED"
    assert raised.value.item.current_stage == "dispatch"
    assert raised.value.item.error == "GitHub workflow dispatch failed"
    assert [
        call.kwargs["state"] for call in github_deployment.create_status.call_args_list
    ] == ["queued", "failure"]


def test_catalog_services_include_deployment_readiness(client):
    response = client.get("/api/catalog/services")
    assert response.status_code == 200
    service = next(
        item
        for item in response.json()["services"]
        if item["service_name"] == "eng-platform-api"
    )
    assert service["deployment_ready"] is True
    assert service["deployment_blockers"] == []


def test_deployment_readiness_reports_missing_fields():
    from eng_platform_api.models import CatalogService, ServiceDeploymentConfig
    from eng_platform_api.services.catalog import deployment_blockers

    service = CatalogService(
        service_name="blocked-svc",
        repository="",
        owner="platform",
        project_id="",
        region="",
        deployment=ServiceDeploymentConfig(
            enabled=False,
            workflow_file="",
            image_name="",
            artifact_repository="",
            build_context="",
            health_path="",
        ),
    )

    blockers = deployment_blockers(service)
    assert "deployment.enabled is false" in blockers
    assert "repository is required" in blockers
    assert "deployment.workflow_file is required" in blockers


def test_create_deployment_rejects_service_that_is_not_deployment_ready(client):
    from eng_platform_api.models import CatalogService, ServiceDeploymentConfig

    blocked = CatalogService(
        service_name="blocked-svc",
        repository="diegomad14/blocked-svc",
        owner="platform",
        project_id="cgm-assistant-prod",
        region="us-central1",
        deployment=ServiceDeploymentConfig(
            enabled=False,
            workflow_file="",
            image_name="blocked-svc",
            artifact_repository="cgm-sanplat-repo",
            build_context=".",
            health_path="/health",
        ),
        deployment_ready=False,
        deployment_blockers=["deployment.enabled is false"],
    )
    with (
        mock.patch(
            "eng_platform_api.routers.deployments.catalog.get_service",
            return_value=blocked,
        ),
        mock.patch(
            "eng_platform_api.routers.deployments.github_deployments.get_tag"
        ) as get_tag,
    ):
        response = client.post(
            "/api/services/blocked-svc/deployments",
            json={"tag": "v1.0.0"},
        )

    assert response.status_code == 409
    assert "not ready for platform deploy" in response.json()["detail"]
    get_tag.assert_not_called()


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
    live = _succeeded_deployment(tag="v0.5.0", created_at="2026-07-16T12:00:00+00:00")
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


def test_list_tags_refetches_from_github_after_cache_ttl_expires():
    """A tag pushed after the last poll must show up once the short-lived
    in-process cache expires, instead of staying hidden for the full TTL
    with no way to invalidate it (tags are created externally by
    semantic-release, outside this API)."""
    from eng_platform_api.services import github_deployments

    repository = "diegomad14/cache-ttl-test-repo"
    github_deployments._tag_metadata_cache.pop((repository, 0, 10), None)

    stale_tags = [SimpleNamespace(name="v1.0.0", commit=SimpleNamespace(sha="a" * 40))]
    fresh_tags = [
        SimpleNamespace(name="v1.1.0", commit=SimpleNamespace(sha="b" * 40)),
        SimpleNamespace(name="v1.0.0", commit=SimpleNamespace(sha="a" * 40)),
    ]
    repo = mock.MagicMock()
    repo.get_commit.side_effect = RuntimeError("no commit metadata in test")
    github = mock.MagicMock()
    github.get_repo.return_value = repo

    try:
        with (
            mock.patch.object(github_deployments.config, "mock_mode", False),
            mock.patch.object(github_deployments, "github_client", return_value=github),
        ):
            repo.get_tags.return_value = stale_tags
            with mock.patch.object(github_deployments, "monotonic", return_value=0.0):
                first = github_deployments.list_tags(
                    repository, "some-service", limit=10
                )
            assert {tag.name for tag in first.items} == {"v1.0.0"}

            repo.get_tags.return_value = fresh_tags
            with mock.patch.object(github_deployments, "monotonic", return_value=10.0):
                still_cached = github_deployments.list_tags(
                    repository, "some-service", limit=10
                )
            assert {tag.name for tag in still_cached.items} == {"v1.0.0"}

            with mock.patch.object(github_deployments, "monotonic", return_value=31.0):
                after_ttl = github_deployments.list_tags(
                    repository, "some-service", limit=10
                )
            assert {tag.name for tag in after_ttl.items} == {"v1.0.0", "v1.1.0"}
    finally:
        github_deployments._tag_metadata_cache.pop((repository, 0, 10), None)


def test_start_rollback_mock_mode_returns_synthetic_item():
    from eng_platform_api.services import catalog, github_deployments

    service = catalog.get_service("eng-platform-api")
    assert service is not None
    target = _succeeded_deployment()

    with mock.patch.object(github_deployments.config, "mock_mode", True):
        item = github_deployments.start_rollback(
            service=service, target=target, requested_by="diegomad14"
        )

    assert item.kind == "rollback"
    assert item.tag == target.tag
    assert item.sha == target.sha
    assert item.status == "QUEUED"
    assert item.stages[0].key == "rollback"


def test_start_rollback_dispatches_with_independent_service_configuration():
    from eng_platform_api.services import catalog, github_deployments

    service = catalog.get_service("cgm-sanplat-web")
    assert service is not None
    target = _succeeded_deployment(tag="v0.4.0")
    repository = mock.MagicMock()
    github_deployment = mock.MagicMock(id=88)
    repository.create_deployment.return_value = github_deployment
    workflow = repository.get_workflow.return_value
    github = mock.MagicMock()
    github.get_repo.return_value = repository

    with (
        mock.patch.object(github_deployments.config, "mock_mode", False),
        mock.patch.object(github_deployments, "github_client", return_value=github),
    ):
        item = github_deployments.start_rollback(
            service=service,
            target=target,
            requested_by="diegomad14",
        )

    assert item.id == "88"
    assert item.kind == "rollback"
    repository.get_workflow.assert_called_once_with("platform-rollback.yml")
    inputs = workflow.create_dispatch.call_args.kwargs["inputs"]
    assert inputs["service_name"] == "cgm-sanplat-web"
    assert inputs["target_tag"] == target.tag
    assert inputs["target_revision"] == target.production_revision
    assert inputs["project_id"] == "cgm-assistant-prod"


def test_retry_dispatch_reuses_github_deployment_and_release_identity():
    from eng_platform_api.services import catalog, github_deployments

    service = catalog.get_service("eng-platform-api")
    assert service is not None
    item = _deployment().model_copy(
        update={
            "id": "73",
            "github_deployment_id": 73,
            "status": "FAILED",
            "current_stage": "dispatch",
            "error": "GitHub workflow dispatch failed",
        }
    )
    github_deployment = mock.MagicMock(id=73)
    repository = mock.MagicMock()
    repository.get_deployment.return_value = github_deployment
    workflow = repository.get_workflow.return_value
    github = mock.MagicMock()
    github.get_repo.return_value = repository

    with (
        mock.patch.object(github_deployments.config, "mock_mode", False),
        mock.patch.object(github_deployments, "github_client", return_value=github),
    ):
        retried = github_deployments.retry_dispatch(service=service, item=item)

    assert retried.id == "73"
    assert retried.github_deployment_id == 73
    assert retried.tag == "v0.5.0"
    assert retried.sha == "a" * 40
    assert retried.status == "QUEUED"
    assert retried.error == ""
    repository.create_deployment.assert_not_called()
    repository.get_deployment.assert_called_once_with(73)
    assert workflow.create_dispatch.call_args.kwargs["ref"] == "v0.5.0"
    assert (
        workflow.create_dispatch.call_args.kwargs["inputs"]["github_deployment_id"]
        == "73"
    )


def test_refresh_recaptures_production_revision_after_run_id_already_cached():
    """A poll made while the deploy was still running caches github_run_id
    before production_revision is posted; a later poll must still pick it up
    instead of short-circuiting on the cached run id."""
    from eng_platform_api.services import github_deployments

    item = DeploymentItem(
        id="300",
        service_name="eng-platform-api",
        repository="diegomad14/gcp-engineering-platform-api",
        tag="v0.9.0",
        sha="c" * 40,
        status="PROMOTING",
        current_stage="promote",
        stages=github_deployments.default_stages(),
        created_at="2026-07-17T12:00:00+00:00",
        updated_at="2026-07-17T12:00:00+00:00",
        github_deployment_id=900,
        github_run_id=901,
    )
    statuses_after_completion = [
        SimpleNamespace(
            log_url="",
            target_url="",
            description="production_revision=eng-platform-api-00099-zzz",
            environment_url="https://prod.example",
        ),
    ]
    run = SimpleNamespace(
        id=901,
        html_url="https://github.com/diegomad14/repo/actions/runs/901",
        updated_at=datetime(2026, 7, 17, 12, 6, 0),
        conclusion="success",
        jobs=lambda: [],
    )
    repo = mock.MagicMock()
    repo.get_deployment.return_value.get_statuses.return_value = (
        statuses_after_completion
    )
    repo.get_workflow_run.return_value = run
    github = mock.MagicMock()
    github.get_repo.return_value = repo

    with (
        mock.patch.object(github_deployments.config, "mock_mode", False),
        mock.patch.object(github_deployments, "github_client", return_value=github),
    ):
        result = github_deployments.refresh(item)

    assert result.production_revision == "eng-platform-api-00099-zzz"
    assert result.status == "SUCCEEDED"
    repo.get_deployment.assert_called_once_with(900)


def test_refresh_does_not_recheck_statuses_once_production_revision_known():
    """Once production_revision is captured, refresh should not keep polling
    GitHub Deployment statuses on every subsequent call."""
    from eng_platform_api.services import github_deployments

    item = DeploymentItem(
        id="301",
        service_name="eng-platform-api",
        repository="diegomad14/gcp-engineering-platform-api",
        tag="v0.9.0",
        sha="c" * 40,
        status="SUCCEEDED",
        current_stage="complete",
        stages=github_deployments.default_stages(),
        created_at="2026-07-17T12:00:00+00:00",
        updated_at="2026-07-17T12:00:00+00:00",
        github_deployment_id=900,
        github_run_id=901,
        production_revision="eng-platform-api-00099-zzz",
    )
    run = SimpleNamespace(
        id=901,
        html_url="https://github.com/diegomad14/repo/actions/runs/901",
        updated_at=datetime(2026, 7, 17, 12, 6, 0),
        conclusion="success",
        jobs=lambda: [],
    )
    repo = mock.MagicMock()
    repo.get_workflow_run.return_value = run
    github = mock.MagicMock()
    github.get_repo.return_value = repo

    with (
        mock.patch.object(github_deployments.config, "mock_mode", False),
        mock.patch.object(github_deployments, "github_client", return_value=github),
    ):
        github_deployments.refresh(item)

    repo.get_deployment.assert_not_called()


def test_refresh_marks_standalone_rollback_deployment_as_rolled_back():
    from eng_platform_api.services import github_deployments

    item = DeploymentItem(
        id="200",
        service_name="eng-platform-api",
        repository="diegomad14/gcp-engineering-platform-api",
        tag="v0.4.0",
        sha="b" * 40,
        kind="rollback",
        status="QUEUED",
        current_stage="queued",
        stages=github_deployments.default_stages("rollback"),
        created_at="2026-07-17T12:00:00+00:00",
        updated_at="2026-07-17T12:00:00+00:00",
        github_run_id=555,
    )
    job = SimpleNamespace(
        name="Rollback production",
        status="completed",
        conclusion="success",
        started_at=datetime(2026, 7, 17, 12, 0, 0),
        completed_at=datetime(2026, 7, 17, 12, 1, 0),
        html_url="https://github.com/diegomad14/repo/actions/runs/555/job/1",
    )
    run = SimpleNamespace(
        id=555,
        html_url="https://github.com/diegomad14/repo/actions/runs/555",
        updated_at=datetime(2026, 7, 17, 12, 1, 0),
        conclusion="success",
        jobs=lambda: [job],
    )
    repo = mock.MagicMock()
    repo.get_workflow_run.return_value = run
    github = mock.MagicMock()
    github.get_repo.return_value = repo

    with (
        mock.patch.object(github_deployments.config, "mock_mode", False),
        mock.patch.object(github_deployments, "github_client", return_value=github),
    ):
        result = github_deployments.refresh(item)

    assert result.status == "ROLLED_BACK"
    assert result.current_stage == "rollback"
    assert result.stages[0].status == "succeeded"
    assert result.stages[0].details == job.html_url


def test_refresh_still_detects_embedded_auto_rollback_on_deploy_kind_item():
    from eng_platform_api.services import github_deployments

    item = _deployment()  # kind="deploy" by default
    item.github_run_id = 556
    rollback_job = SimpleNamespace(
        name="Rollback production",
        status="completed",
        conclusion="failure",
        started_at=datetime(2026, 7, 17, 12, 0, 0),
        completed_at=datetime(2026, 7, 17, 12, 1, 0),
        html_url="https://github.com/diegomad14/repo/actions/runs/556/job/1",
    )
    unrelated_job = SimpleNamespace(
        name="Setup runner",
        status="completed",
        conclusion="success",
        started_at=datetime(2026, 7, 17, 11, 59, 0),
        completed_at=datetime(2026, 7, 17, 11, 59, 30),
        html_url="https://github.com/diegomad14/repo/actions/runs/556/job/0",
    )
    run = SimpleNamespace(
        id=556,
        html_url="https://github.com/diegomad14/repo/actions/runs/556",
        updated_at=datetime(2026, 7, 17, 12, 1, 0),
        conclusion="failure",
        jobs=lambda: [unrelated_job, rollback_job],
    )
    repo = mock.MagicMock()
    repo.get_workflow_run.return_value = run
    github = mock.MagicMock()
    github.get_repo.return_value = repo

    with (
        mock.patch.object(github_deployments.config, "mock_mode", False),
        mock.patch.object(github_deployments, "github_client", return_value=github),
    ):
        result = github_deployments.refresh(item)

    assert result.status == "ROLLBACK_FAILED"
    assert result.current_stage == "rollback"
    # The deploy pipeline's own stages stay untouched by the embedded rollback job.
    assert all(stage.status == "pending" for stage in result.stages)


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
