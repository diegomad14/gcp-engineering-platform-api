"""Releases router — service-oriented webhook and history."""

from typing import Optional
from datetime import datetime, timezone
import hashlib
import json
from threading import Lock
from time import monotonic

from fastapi import APIRouter, HTTPException, Query, Request

from ..config import config
from ..models import ReleaseCreateRequest, ReleaseItem, ReleaseSummary
from ..services import (
    github_actions,
    releases_store,
    execution_control_store,
    local_release_policy,
)
from ..security import require_deployer

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
def register_release(payload: ReleaseCreateRequest, request: Request):
    """Register one independent release row for every payload service."""
    controlled = bool(payload.release_id) or any(
        local_release_policy.adopted(item.service_name) for item in payload.services
    )
    if payload.release_group_id or len(payload.services) != 1:
        raise HTTPException(
            status_code=409,
            detail="New release registrations must contain exactly one service and no release group",
        )
    if controlled and not config.mock_mode:
        actor = require_deployer(request)
        service_name = payload.services[0].service_name
        try:
            local_release_policy.require_enabled(service_name)
        except local_release_policy.LocalReleasePolicyError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        intent_id = request.headers.get("X-Release-Intent", "")
        intent = execution_control_store.get_intent(intent_id) if intent_id else None
        if intent is None or intent.status != "INTENDED":
            raise HTTPException(
                status_code=409, detail="A live durable registration intent is required"
            )
        context = intent.context
        if (
            context.actor_id.lower() != actor.lower()
            or context.operation != "register"
            or context.service_name != service_name
            or context.release_id != payload.release_id
            or context.repository != payload.repository
            or context.source_sha != payload.source_sha
            or context.artifact_digest != payload.artifact_digest
            or context.tag != payload.version
        ):
            raise HTTPException(
                status_code=403,
                detail="Registration does not match its authorized intent",
            )
        lease = execution_control_store.get_lease(intent.scope_key)
        if (
            lease is None
            or lease.status != "HELD"
            or lease.lease_id != intent.lease_id
            or lease.generation != intent.lease_generation
            or datetime.fromisoformat(lease.expires_at.replace("Z", "+00:00"))
            <= datetime.now(timezone.utc)
        ):
            raise HTTPException(
                status_code=409, detail="Registration lease is no longer active"
            )
        payload_hash = hashlib.sha256(
            json.dumps(
                payload.model_dump(exclude_unset=True),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        expected_effect = {
            "context": context.model_dump(),
            "scope_key": intent.scope_key,
            "stage": "register",
            "effect_key": f"platform-registration:{payload.status}",
            "command": ["POST", str(request.url), payload_hash],
        }
        expected_digest = hashlib.sha256(
            json.dumps(
                expected_effect,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode()
        ).hexdigest()
        if intent.effect_digest != expected_digest:
            raise HTTPException(
                status_code=409,
                detail="Registration payload differs from durable intent",
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
