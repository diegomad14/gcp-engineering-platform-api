"""GitHub release and CI evidence for catalog services."""

from typing import Any

from ..config import config
from ..models import (
    CatalogService,
    DeploymentItem,
    QualityCheck,
    QualityCheckStatus,
    QualityGateStatus,
    QualityProject,
    ReleaseItem,
    ReleaseServiceAction,
    ReleaseSummary,
)
from . import catalog, deployment_store, github_deployments


def _fetch_recent_releases(repository: str, limit: int = 5) -> list[object]:
    repo = github_deployments.github_client().get_repo(repository)
    releases = repo.get_releases()
    return [release for index, release in enumerate(releases) if index < limit]


def _release_item(
    service: CatalogService,
    release: object,
    deployments: list[DeploymentItem],
) -> ReleaseItem:
    tag = getattr(release, "tag_name", "") or ""
    deployment = next((item for item in deployments if item.tag == tag), None)
    status = "released"
    action: ReleaseServiceAction = "released"
    revision = ""
    source_url = getattr(release, "html_url", "") or ""
    if deployment:
        source_url = deployment.github_run_url or source_url
        revision = deployment.production_revision
        if deployment.status == "SUCCEEDED":
            status, action = "promoted", "deployed"
        elif deployment.status == "ROLLED_BACK":
            status, action = "rolled_back", "rolled_back"
        elif deployment.status in {"FAILED", "ROLLBACK_FAILED"}:
            status, action = "failed", "missing"
        else:
            status, action = "deploying", "missing"
    created_at = getattr(release, "published_at", None) or getattr(
        release, "created_at", None
    )
    return ReleaseItem(
        service_name=service.service_name,
        repository=service.repository,
        version=tag,
        status=status,
        revision=revision,
        action=action,
        github_run_url=source_url,
        created_at=github_deployments._iso(created_at),
    )


def get_release_summary() -> ReleaseSummary:
    if config.mock_mode:
        fallback = _fallback_releases()
        return ReleaseSummary(recent=fallback, total_releases=len(fallback))

    recent: list[ReleaseItem] = []
    repositories = sorted(
        {
            service.repository
            for service in catalog.get_services().services
            if service.repository
        }
    )
    for repository in repositories:
        services = catalog.get_services_by_repository(repository)
        try:
            releases = _fetch_recent_releases(repository)
        except Exception:
            continue
        deployments = {
            service.service_name: deployment_store.list_for_service(
                service.service_name, limit=100
            )
            for service in services
        }
        for release in releases:
            tag = getattr(release, "tag_name", "") or ""
            if not github_deployments._SEMVER.fullmatch(tag):
                continue
            for service in services:
                recent.append(
                    _release_item(
                        service,
                        release,
                        deployments[service.service_name],
                    )
                )

    recent.sort(key=lambda release: release.created_at, reverse=True)
    recent = recent[:20]
    return ReleaseSummary(recent=recent, total_releases=len(recent))


def _get_ci_workflow(repo: Any) -> Any | None:
    for workflow_file in ("ci.yml", "pr-check.yml"):
        try:
            return repo.get_workflow(workflow_file)
        except Exception:
            continue
    return None


def _ci_status(
    conclusion: str, run_status: str
) -> tuple[QualityGateStatus, QualityCheckStatus]:
    if run_status != "completed":
        return "RUNNING", "SKIPPED"
    if conclusion == "success":
        return "PASSED", "PASSED"
    return "FAILED", "FAILED"


def get_ci_quality_project(service: CatalogService) -> QualityProject | None:
    """Return latest default-branch CI evidence when no normalized report exists."""
    if config.mock_mode or not service.repository:
        return None
    try:
        repo = github_deployments.github_client().get_repo(service.repository)
        workflow = _get_ci_workflow(repo)
        if workflow is None:
            return None
        run = next(iter(workflow.get_runs(branch=repo.default_branch)), None)
    except Exception:
        return None
    if run is None:
        return None

    run_status = getattr(run, "status", "") or ""
    conclusion = getattr(run, "conclusion", "") or ""
    status, check_status = _ci_status(conclusion, run_status)

    return QualityProject(
        project_key=service.service_name,
        service_name=service.service_name,
        repository=service.repository,
        commit_sha=getattr(run, "head_sha", "") or "",
        branch=getattr(run, "head_branch", "") or repo.default_branch,
        profile=service.quality.profile or "",
        quality_gate_status=status,
        coverage=None,
        url=getattr(run, "html_url", "") or "",
        updated_at=github_deployments._iso(
            getattr(run, "updated_at", None) or getattr(run, "created_at", None)
        ),
        checks=[
            QualityCheck(
                name="GitHub Actions CI",
                category="ci",
                status=check_status,
                details=(
                    "Default-branch workflow is still running"
                    if status == "RUNNING"
                    else f"Workflow conclusion: {conclusion or 'unknown'}"
                ),
            )
        ],
        evidence_source="github-actions",
    )


def _fallback_releases() -> list[ReleaseItem]:
    return [
        ReleaseItem(
            service_name="cgm-sanplat-api",
            repository="diegomad14/parametrizacion-correos-cgm",
            version="v0.4.3",
            status="promoted",
            revision="cgm-sanplat-api-00173-5cs",
            action="promoted",
            github_run_url="https://github.com/diegomad14/parametrizacion-correos-cgm/actions",
            created_at="2026-07-07T15:57:39Z",
        ),
        ReleaseItem(
            service_name="cgm-sanplat-web",
            repository="diegomad14/parametrizacion-correos-cgm",
            version="v0.4.3",
            status="promoted",
            revision="cgm-sanplat-web-00088-bx5",
            action="promoted",
            github_run_url="https://github.com/diegomad14/parametrizacion-correos-cgm/actions",
            created_at="2026-07-07T15:57:39Z",
        ),
    ]
