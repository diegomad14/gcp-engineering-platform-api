"""GitHub Actions service — discovered runs expanded to service rows."""

import json
import os
import urllib.request
from typing import Any

from ..config import config
from ..models import ReleaseItem, ReleaseSummary
from . import catalog

_GITHUB_API = "https://api.github.com"


def _github_request(path: str) -> Any:
    req = urllib.request.Request(f"{_GITHUB_API}{path}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "eng-platform-api")
    token = config.github.token or os.getenv("GITHUB_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.loads(response.read())
    except Exception:
        return None


def _fetch_recent_runs(repository: str, limit: int = 5) -> list[dict]:
    data = _github_request(
        f"/repos/{repository}/actions/runs?per_page={limit}&status=completed"
    )
    return data.get("workflow_runs", []) if data else []


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
        for run in _fetch_recent_runs(repository):
            for service in services:
                recent.append(
                    ReleaseItem(
                        service_name=service.service_name,
                        repository=repository,
                        version=run.get("head_branch", "")[:40],
                        status="completed"
                        if run.get("conclusion") == "success"
                        else "failed",
                        revision="",
                        action="missing",
                        github_run_url=run.get("html_url", ""),
                        created_at=run.get("created_at", ""),
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
