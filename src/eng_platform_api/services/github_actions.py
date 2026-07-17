"""Legacy release summary projected from GitHub Actions.

New deployment screens use ``github_deployments`` directly. This adapter keeps
the existing releases endpoint backward compatible without a second HTTP
client or hand-written authentication implementation.
"""

from ..config import config
from ..models import ReleaseItem, ReleaseSummary
from . import catalog, github_deployments


def _fetch_recent_runs(repository: str, limit: int = 5) -> list[object]:
    repo = github_deployments.github_client().get_repo(repository)
    runs = repo.get_workflow_runs(status="completed")
    return [run for index, run in enumerate(runs) if index < limit]


def get_release_summary() -> ReleaseSummary:
    if config.mock_mode:
        return ReleaseSummary(recent=_fallback_releases(), total_releases=0)

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
            runs = _fetch_recent_runs(repository)
        except Exception:
            continue
        for run in runs:
            for service in services:
                conclusion = getattr(run, "conclusion", "")
                recent.append(
                    ReleaseItem(
                        service_name=service.service_name,
                        repository=repository,
                        version=(getattr(run, "head_branch", "") or "")[:40],
                        status="completed" if conclusion == "success" else "failed",
                        revision="",
                        action="missing",
                        github_run_url=getattr(run, "html_url", "") or "",
                        created_at=github_deployments._iso(
                            getattr(run, "created_at", None)
                        ),
                    )
                )

    recent.sort(key=lambda release: release.created_at, reverse=True)
    recent = recent[:20]
    return ReleaseSummary(recent=recent, total_releases=len(recent))


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
