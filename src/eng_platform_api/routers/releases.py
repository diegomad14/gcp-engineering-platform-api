"""Releases router — service-oriented webhook and history."""

from typing import Optional
from threading import Lock
from time import monotonic

from fastapi import APIRouter, HTTPException, Query, Request

from ..config import config
from ..services.metadata_scope import cache_identity
from ..models import ReleaseCreateRequest, ReleaseItem, ReleaseSummary
from ..services import (
    github_actions,
    releases_store,
)

router = APIRouter(prefix="/api/releases", tags=["releases"])
_SUMMARY_CACHE_TTL_SECONDS = 60
_summary_cache: tuple[float, ReleaseSummary, object] | None = None
_summary_cache_lock = Lock()


def _invalidate_summary_cache() -> None:
    global _summary_cache
    with _summary_cache_lock:
        _summary_cache = None


def _release_identity(item: ReleaseItem) -> tuple[str, ...]:
    return (item.service_name, item.repository, item.version)


@router.post("/", response_model=list[ReleaseItem], status_code=201)
def register_release(payload: ReleaseCreateRequest, request: Request):
    """Register one independent release row for every payload service."""
    if payload.release_group_id or len(payload.services) != 1:
        raise HTTPException(
            status_code=409,
            detail="New release registrations must contain exactly one service and no release group",
        )
    try:
        releases = releases_store.save_release(payload)
    except releases_store.ReleaseConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _invalidate_summary_cache()
    return releases


@router.get("/", response_model=ReleaseSummary)
def list_releases(
    service_name: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    """List recent releases, optionally filtered by service."""
    from ..services.metadata_scope import visible_services

    scope = visible_services.get()
    if scope is not None:
        names = (
            [service_name]
            if service_name in scope
            else sorted(scope)
            if service_name is None
            else []
        )
        stored = [
            item
            for name in names
            for item in releases_store.get_releases(service_name=name, limit=limit)
        ]
        stored.sort(key=lambda item: item.created_at, reverse=True)
        return ReleaseSummary(
            recent=stored[:limit],
            total_releases=sum(
                releases_store.count_releases(service_name=name) for name in names
            ),
        )
    stored = releases_store.get_releases(service_name=service_name, limit=limit)
    total = releases_store.count_releases(service_name=service_name)
    return ReleaseSummary(recent=stored, total_releases=total)


@router.get("/summary", response_model=ReleaseSummary)
def get_release_summary():
    """Merge persisted service rows with GitHub semantic releases."""
    global _summary_cache
    source = cache_identity()
    with _summary_cache_lock:
        now = monotonic()
        if (
            not config.mock_mode
            and _summary_cache
            and _summary_cache[2] == source
            and now - _summary_cache[0] < _SUMMARY_CACHE_TTL_SECONDS
        ):
            return _summary_cache[1]

        summary = _build_release_summary()
        if not config.mock_mode:
            _summary_cache = (monotonic(), summary, source)
        return summary


def _build_release_summary() -> ReleaseSummary:
    from ..services.metadata_scope import visible_services

    scope = visible_services.get()
    recent = (
        releases_store.get_releases(limit=100)
        if scope is None
        else [
            item
            for name in sorted(scope)
            for item in releases_store.get_releases(service_name=name, limit=100)
        ]
    )
    github = github_actions.get_release_summary()
    seen = {_release_identity(item) for item in recent}
    for item in github.recent:
        if scope is not None and item.service_name not in scope:
            continue
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
