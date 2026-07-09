"""Releases router — webhook for registering releases and querying history."""

from typing import Optional

from fastapi import APIRouter, Query

from ..models import ReleaseCreateRequest, ReleaseItem, ReleaseSummary
from ..services import github_actions, releases_store

router = APIRouter(prefix="/api/releases", tags=["releases"])


@router.post("/", response_model=ReleaseItem, status_code=201)
async def register_release(payload: ReleaseCreateRequest):
    """Register a new release, promotion, or rollback.

    Called by CI/CD workflows (release.yml, promote-prod.yml, rollback-prod.yml)
    to record every deployment event in the platform release history.
    """
    return releases_store.save_release(payload)


@router.get("/", response_model=ReleaseSummary)
async def list_releases(
    app_id: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    """List recent releases, optionally filtered by application."""
    stored = releases_store.get_releases(app_id=app_id, limit=limit)
    total = releases_store.count_releases(app_id=app_id)
    return ReleaseSummary(recent=stored, total_releases=total)


@router.get("/summary", response_model=ReleaseSummary)
async def get_release_summary():
    """Get release activity: stored releases + GitHub workflow runs."""
    stored = releases_store.get_releases(limit=10)
    github = github_actions.get_release_summary()

    # Merge: stored releases first, then deduplicate by github_run_url
    seen_urls = {r.github_run_url for r in stored if r.github_run_url}
    for item in github.recent:
        if item.github_run_url not in seen_urls:
            stored.append(item)
            seen_urls.add(item.github_run_url)

    stored.sort(key=lambda r: r.created_at, reverse=True)
    return ReleaseSummary(
        recent=stored[:20],
        total_releases=releases_store.count_releases(),
    )


@router.get("/{app_id}/latest", response_model=ReleaseItem)
async def get_latest_release(app_id: str):
    """Get the latest release for a specific application."""
    from fastapi import HTTPException

    latest = releases_store.get_latest(app_id)
    if latest is None:
        raise HTTPException(status_code=404, detail=f"No releases found for '{app_id}'")
    return latest
