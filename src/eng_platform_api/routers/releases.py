"""Releases router — service-oriented webhook and history."""

from typing import Optional
from threading import Lock
from time import monotonic

from fastapi import APIRouter, HTTPException, Query

from ..config import config
from ..models import ReleaseCreateRequest, ReleaseItem, ReleaseSummary
from ..services import github_actions, releases_store

router = APIRouter(prefix="/api/releases", tags=["releases"])
_SUMMARY_CACHE_TTL_SECONDS = 60
_summary_cache: tuple[float, ReleaseSummary] | None = None
_summary_cache_lock = Lock()


def _invalidate_summary_cache() -> None:
    global _summary_cache
    with _summary_cache_lock:
        _summary_cache = None


def _release_identity(item: ReleaseItem) -> tuple[str, ...]:
    return (item.service_name, item.repository, item.version)


@router.post("/", response_model=list[ReleaseItem], status_code=201)
def register_release(payload: ReleaseCreateRequest):
    """Register one independent release row for every payload service."""
    releases = releases_store.save_release(payload)
    _invalidate_summary_cache()
    return releases


@router.get("/", response_model=ReleaseSummary)
def list_releases(
    service_name: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    """List recent releases, optionally filtered by service."""
    stored = releases_store.get_releases(service_name=service_name, limit=limit)
    total = releases_store.count_releases(service_name=service_name)
    return ReleaseSummary(recent=stored, total_releases=total)


@router.get("/summary", response_model=ReleaseSummary)
def get_release_summary():
    """Merge persisted service rows with GitHub semantic releases."""
    global _summary_cache
    with _summary_cache_lock:
        now = monotonic()
        if (
            not config.mock_mode
            and _summary_cache
            and now - _summary_cache[0] < _SUMMARY_CACHE_TTL_SECONDS
        ):
            return _summary_cache[1]

        summary = _build_release_summary()
        if not config.mock_mode:
            _summary_cache = (monotonic(), summary)
        return summary


def _build_release_summary() -> ReleaseSummary:
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
def get_latest_release(service_name: str):
    """Get the latest release for a specific service."""
    latest = releases_store.get_latest(service_name)
    if latest is None:
        raise HTTPException(
            status_code=404, detail=f"No releases found for '{service_name}'"
        )
    return latest
