"""Tests for GitHub-backed release and quality evidence."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from eng_platform_api.models import CatalogResponse, CatalogService, DeploymentItem
from eng_platform_api.services import github_actions


def _service() -> CatalogService:
    return CatalogService(
        service_name="test-api",
        repository="test-org/test-api",
        owner="platform",
        project_id="test-project",
        region="us-central1",
    )


def test_release_summary_uses_semver_releases_and_deployment_state():
    service = _service()
    release = SimpleNamespace(
        tag_name="v1.2.3",
        html_url="https://github.com/test-org/test-api/releases/tag/v1.2.3",
        published_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    branch_release = SimpleNamespace(tag_name="main")
    repo = mock.MagicMock()
    repo.get_releases.return_value = [release, branch_release]
    deployment = DeploymentItem(
        id="deployment-1",
        service_name=service.service_name,
        repository=service.repository,
        tag="v1.2.3",
        status="SUCCEEDED",
        production_revision="test-api-00001-abc",
        github_run_url="https://github.com/test-org/test-api/actions/runs/1",
    )

    with (
        mock.patch.object(github_actions.config, "mock_mode", False),
        mock.patch.object(
            github_actions.catalog,
            "get_services",
            return_value=CatalogResponse(services=[service], total=1),
        ),
        mock.patch.object(
            github_actions.catalog,
            "get_services_by_repository",
            return_value=[service],
        ),
        mock.patch.object(
            github_actions.github_deployments,
            "github_client",
        ) as github_client,
        mock.patch.object(
            github_actions.deployment_store,
            "list_for_service",
            return_value=[deployment],
        ),
    ):
        github_client.return_value.get_repo.return_value = repo
        summary = github_actions.get_release_summary()

    assert summary.total_releases == 1
    assert summary.recent[0].version == "v1.2.3"
    assert summary.recent[0].status == "promoted"
    assert summary.recent[0].action == "deployed"
    assert summary.recent[0].revision == "test-api-00001-abc"


def test_ci_quality_project_reports_unknown_coverage():
    service = _service()
    run = SimpleNamespace(
        status="completed",
        conclusion="success",
        head_sha="a" * 40,
        head_branch="main",
        html_url="https://github.com/test-org/test-api/actions/runs/1",
        updated_at=datetime(2026, 7, 20, tzinfo=timezone.utc),
    )
    repo = mock.MagicMock(default_branch="main")
    repo.get_workflow.return_value.get_runs.return_value = [run]

    with (
        mock.patch.object(github_actions.config, "mock_mode", False),
        mock.patch.object(
            github_actions.github_deployments,
            "github_client",
        ) as github_client,
    ):
        github_client.return_value.get_repo.return_value = repo
        project = github_actions.get_ci_quality_project(service)

    assert project is not None
    assert project.quality_gate_status == "PASSED"
    assert project.coverage is None
    assert project.evidence_source == "github-actions"
