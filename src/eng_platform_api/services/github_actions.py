"""GitHub Actions service — real release history from GitHub API.

Queries:
- GitHub API for recent workflow runs across app repos
- Falls back to simulated data when token is unavailable.
"""

import json
import os
import urllib.request
import urllib.error
from typing import Any

from ..config import config
from ..models import ReleaseItem, ReleaseSummary

_GITHUB_API = "https://api.github.com"
_REPOS = [
    "diegomad14/parametrizacion-correos-cgm",
    "diegomad14/gcp-engineering-platform",
]


def _github_request(path: str) -> Any:
    """Make an authenticated or anonymous GitHub API request."""
    url = f"{_GITHUB_API}{path}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "eng-platform-api")
    token = config.github.token or os.getenv("GITHUB_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _fetch_recent_runs(repo: str, limit: int = 5) -> list[dict]:
    """Fetch recent workflow runs for a repo."""
    data = _github_request(f"/repos/{repo}/actions/runs?per_page={limit}&status=completed")
    if data and "workflow_runs" in data:
        return data["workflow_runs"]
    return []


def _infer_app_name(repo: str) -> str:
    mapping = {
        "diegomad14/parametrizacion-correos-cgm": "CGM Integration Platform",
        "diegomad14/gcp-engineering-platform": "Engineering Platform",
    }
    return mapping.get(repo, repo.split("/")[-1])


def _infer_app_id(repo: str) -> str:
    mapping = {
        "diegomad14/parametrizacion-correos-cgm": "cgm-integration-platform",
        "diegomad14/gcp-engineering-platform": "eng-platform",
    }
    return mapping.get(repo, repo.split("/")[-1])


def get_release_summary() -> ReleaseSummary:
    if config.mock_mode:
        return ReleaseSummary(recent=_fallback_releases(), total_releases=0)

    recent: list[ReleaseItem] = []
    for repo in _REPOS:
        runs = _fetch_recent_runs(repo)
        for run in runs:
            recent.append(ReleaseItem(
                app_id=_infer_app_id(repo),
                app_name=_infer_app_name(repo),
                version=run.get("head_branch", "")[:40],
                status="completed" if run.get("conclusion") == "success" else "failed",
                api_revision="",
                web_revision="",
                github_run_url=run.get("html_url", ""),
                created_at=run.get("created_at", ""),
            ))

    # Sort by date descending
    recent.sort(key=lambda r: r.created_at, reverse=True)
    recent = recent[:10]

    if not recent:
        return ReleaseSummary(recent=_fallback_releases(), total_releases=0)

    return ReleaseSummary(recent=recent, total_releases=len(recent))


def _fallback_releases() -> list[ReleaseItem]:
    return [
        ReleaseItem(
            app_id="cgm-integration-platform",
            app_name="CGM Integration Platform",
            version="v0.4.3",
            status="promoted",
            api_revision="cgm-sanplat-api-00173-5cs",
            web_revision="cgm-sanplat-web-00088-bx5",
            github_run_url="https://github.com/diegomad14/parametrizacion-correos-cgm/actions",
            created_at="2026-07-07T15:57:39Z",
        ),
    ]
