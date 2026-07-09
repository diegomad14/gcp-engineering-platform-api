"""Release history persistence — JSON file store.

No database dependency (aligned with ADR "No Database for MVP").
Thread-safe writes via a reentrant lock.
"""

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models import ReleaseCreateRequest, ReleaseItem, ReleaseSummary, ServiceRevision

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


def save_release(payload: ReleaseCreateRequest) -> ReleaseItem:
    """Persist a release (candidate, promote, or rollback) and return the stored item."""
    now = datetime.now(timezone.utc).isoformat()
    api_revision = ""
    web_revision = ""
    for svc in payload.services:
        if "api" in svc.service_name.lower():
            api_revision = svc.revision
        elif "web" in svc.service_name.lower():
            web_revision = svc.revision

    item = ReleaseItem(
        app_id=payload.app_id,
        app_name=payload.app_name,
        version=payload.version,
        status=payload.status,
        api_revision=api_revision,
        web_revision=web_revision,
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
            "services": [s.model_dump() for s in payload.services],
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
        items.append(ReleaseItem(
            app_id=rec.get("app_id", ""),
            app_name=rec.get("app_name", ""),
            version=rec.get("version", ""),
            status=rec.get("status", ""),
            api_revision=rec.get("api_revision", ""),
            web_revision=rec.get("web_revision", ""),
            github_run_url=rec.get("github_run_url", ""),
            created_at=rec.get("created_at", ""),
        ))
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
