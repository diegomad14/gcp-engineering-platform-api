"""Release history persistence — JSON file store.

No database dependency (aligned with ADR "No Database for MVP").
Thread-safe writes via a reentrant lock.
"""

import json
import os
import threading
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

from pydantic import ValidationError
from ..models import ReleaseCreateRequest, ReleaseItem, ServiceRevision

_DEFAULT_STORE_PATH = Path(os.getenv("RELEASES_STORE_PATH", "data/releases.json"))
_store_lock = threading.RLock()


def _load() -> list[dict]:
    path = _resolve_path()
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _save(records: list[dict]) -> None:
    path = _resolve_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _resolve_path() -> Path:
    return Path(_DEFAULT_STORE_PATH) if _DEFAULT_STORE_PATH.is_absolute() else Path.cwd() / _DEFAULT_STORE_PATH


@lru_cache(maxsize=64)
def _catalog_service_names(app_id: str) -> tuple[str, ...]:
    """Return the catalog release targets for an application."""
    from . import catalog

    aliases = {"eng-platform": "engineering-platform"}
    app = catalog.get_application(aliases.get(app_id, app_id))
    if app is None:
        return ()
    return tuple(target.service_name for target in app.release_targets)


def _normalize_service(service: ServiceRevision) -> ServiceRevision:
    """Mark incomplete service entries without hiding explicit no-op actions."""
    if service.revision or service.action in {"unchanged", "not_included", "missing"}:
        return service
    return ServiceRevision(
        service_name=service.service_name,
        revision="",
        action="missing",
    )


def complete_services(
    app_id: str,
    services: list[ServiceRevision],
    *,
    absent_action: Literal["not_included", "missing"],
) -> list[ServiceRevision]:
    """Combine payload services with the application's catalog targets.

    Catalog order is kept for known services. Payload services that are not yet
    present in the catalog are retained at the end instead of being discarded.
    """
    provided = {
        service.service_name: _normalize_service(service)
        for service in services
    }
    completed: list[ServiceRevision] = []

    for service_name in _catalog_service_names(app_id):
        completed.append(
            provided.pop(
                service_name,
                ServiceRevision(
                    service_name=service_name,
                    revision="",
                    action=absent_action,
                ),
            )
        )

    completed.extend(provided.values())
    return completed


def _legacy_services(record: dict) -> list[ServiceRevision]:
    """Recover revision data from pre-multiservice records when possible."""
    revisions = [
        record.get("api_revision", ""),
        record.get("web_revision", ""),
    ]
    service_names = _catalog_service_names(record.get("app_id", ""))
    release_status = record.get("status", "")
    action = {
        "promoted": "promoted",
        "rolled_back": "rolled_back",
    }.get(release_status, "deployed")

    recovered: list[ServiceRevision] = []
    for revision in revisions:
        if not revision:
            continue
        service_name = next(
            (
                name for name in service_names
                if revision == name or revision.startswith(f"{name}-")
            ),
            "",
        )
        if service_name:
            recovered.append(
                ServiceRevision(
                    service_name=service_name,
                    revision=revision,
                    action=action,
                )
            )
    return recovered


def _stored_service(raw_service: dict) -> ServiceRevision:
    """Read stored service data defensively across contract versions."""
    try:
        return ServiceRevision(**raw_service)
    except (TypeError, ValidationError):
        return ServiceRevision(
            service_name=str(raw_service.get("service_name", "")),
            revision=str(raw_service.get("revision", "")),
            action="missing",
        )


def release_item_from_record(record: dict) -> ReleaseItem:
    """Build the public release contract from current or legacy storage."""
    raw_services = record.get("services") or []
    if raw_services:
        services = [_stored_service(service) for service in raw_services]
        absent_action = "not_included"
    else:
        services = _legacy_services(record)
        absent_action = "missing"

    return ReleaseItem(
        app_id=record.get("app_id", ""),
        app_name=record.get("app_name", ""),
        version=record.get("version", ""),
        status=record.get("status", ""),
        services=complete_services(
            record.get("app_id", ""),
            services,
            absent_action=absent_action,
        ),
        github_run_url=record.get("github_run_url", ""),
        created_at=record.get("created_at", ""),
    )


def save_release(payload: ReleaseCreateRequest) -> ReleaseItem:
    """Persist a release (candidate, promote, or rollback) and return the stored item."""
    now = datetime.now(timezone.utc).isoformat()
    services = complete_services(
        payload.app_id,
        payload.services,
        absent_action="not_included" if payload.services else "missing",
    )

    item = ReleaseItem(
        app_id=payload.app_id,
        app_name=payload.app_name,
        version=payload.version,
        status=payload.status,
        services=services,
        github_run_url=payload.github_run_url,
        created_at=now,
    )

    with _store_lock:
        records = _load()
        records.append({
            **item.model_dump(),
            "triggered_by": payload.triggered_by,
            "rollback_from_version": payload.rollback_from_version,
            "notes": payload.notes,
            "services": [service.model_dump() for service in services],
        })
        _save(records)

    return item


def get_releases(app_id: Optional[str] = None, limit: int = 20) -> list[ReleaseItem]:
    with _store_lock:
        records = _load()

    items: list[ReleaseItem] = []
    for rec in reversed(records):  # newest first
        if app_id and rec.get("app_id") != app_id:
            continue
        items.append(release_item_from_record(rec))
    return items[:limit]


def get_latest(app_id: str) -> Optional[ReleaseItem]:
    releases = get_releases(app_id=app_id, limit=1)
    return releases[0] if releases else None


def count_releases(app_id: Optional[str] = None) -> int:
    with _store_lock:
        records = _load()
    if app_id:
        return sum(1 for r in records if r.get("app_id") == app_id)
    return len(records)
