"""Releases router — service-oriented webhook and history."""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from ..models import ReleaseCreateRequest, ReleaseItem, ReleaseSummary
from ..services import github_actions, releases_store

router = APIRouter(prefix="/api/releases", tags=["releases"])


def _release_identity(item: ReleaseItem) -> tuple[str, ...]:
    return (item.service_name, item.repository, item.version)


@router.post("/", response_model=list[ReleaseItem], status_code=201)
async def register_release(payload: ReleaseCreateRequest):
    """Register one independent release row for every payload service."""
    return releases_store.save_release(payload)


@router.get("/", response_model=ReleaseSummary)
async def list_releases(
    service_name: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    """List recent releases, optionally filtered by service."""
    stored = releases_store.get_releases(service_name=service_name, limit=limit)
    total = releases_store.count_releases(service_name=service_name)
    return ReleaseSummary(recent=stored, total_releases=total)


@router.get("/summary", response_model=ReleaseSummary)
async def get_release_summary():
    """Merge persisted service rows with GitHub semantic releases."""
    recent = releases_store.get_releases(limit=100)
    github = github_actions.get_release_summary()
    seen = {_release_identity(item) for item in recent}
    for item in github.recent:
        identity = _release_identity(item)
        if identity not in seen:
            recent.append(item)
            seen.add(identity)

    recent.sort(key=lambda release: release.created_at, reverse=True)
    return ReleaseSummary(recent=recent[:20], total_releases=len(recent))


@router.get("/{service_name}/latest", response_model=ReleaseItem)
async def get_latest_release(service_name: str):
    """Get the latest release for a specific service."""
    latest = releases_store.get_latest(service_name)
    if latest is None:
        raise HTTPException(
            status_code=404, detail=f"No releases found for '{service_name}'"
        )
    return latest
