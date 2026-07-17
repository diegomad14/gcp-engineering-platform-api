"""Minimal deployment request persistence.

GitHub remains the source of truth for execution state. This store only keeps
the platform request identity and the GitHub object references needed to
reconstruct that state after an API restart.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from ..models import DeploymentItem

_DEFAULT_STORE_PATH = Path(
    os.getenv("ENG_PLATFORM_DEPLOYMENT_STORE_PATH", "data/deployments.json")
)
_COLLECTION = os.getenv("ENG_PLATFORM_DEPLOYMENT_FIRESTORE_COLLECTION", "")
_lock = threading.RLock()


def _path() -> Path:
    return (
        _DEFAULT_STORE_PATH
        if _DEFAULT_STORE_PATH.is_absolute()
        else Path.cwd() / _DEFAULT_STORE_PATH
    )


def _local_load() -> list[dict[str, Any]]:
    path = _path()
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _local_save(items: list[dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2), encoding="utf-8")


def _firestore_collection():
    if not _COLLECTION:
        return None
    from google.cloud import firestore

    return firestore.Client().collection(_COLLECTION)


def save(item: DeploymentItem, idempotency_key: str) -> DeploymentItem:
    collection = _firestore_collection()
    if collection is not None:
        document = collection.document(item.id)
        existing = document.get()
        previous_key = (
            (existing.to_dict() or {}).get("idempotency_key", "")
            if existing.exists
            else ""
        )
        record = {
            **item.model_dump(),
            "idempotency_key": idempotency_key or previous_key,
        }
        document.set(record)
        return item
    with _lock:
        current = _local_load()
        previous = next((r for r in current if r.get("id") == item.id), {})
        record = {
            **item.model_dump(),
            "idempotency_key": idempotency_key or previous.get("idempotency_key", ""),
        }
        records = [r for r in current if r.get("id") != item.id]
        records.append(record)
        _local_save(records)
    return item


def get(deployment_id: str) -> DeploymentItem | None:
    collection = _firestore_collection()
    if collection is not None:
        snapshot = collection.document(deployment_id).get()
        return DeploymentItem(**snapshot.to_dict()) if snapshot.exists else None
    with _lock:
        record = next((r for r in _local_load() if r.get("id") == deployment_id), None)
    return DeploymentItem(**record) if record else None


def find_by_idempotency_key(key: str) -> DeploymentItem | None:
    if not key:
        return None
    collection = _firestore_collection()
    if collection is not None:
        snapshots = collection.where("idempotency_key", "==", key).limit(1).stream()
        record = next((snapshot.to_dict() for snapshot in snapshots), None)
        return DeploymentItem(**record) if record else None
    with _lock:
        record = next(
            (r for r in _local_load() if r.get("idempotency_key") == key), None
        )
    return DeploymentItem(**record) if record else None


def list_for_service(service_name: str, limit: int = 20) -> list[DeploymentItem]:
    collection = _firestore_collection()
    if collection is not None:
        snapshots = collection.where("service_name", "==", service_name).stream()
        records = [snapshot.to_dict() for snapshot in snapshots]
    else:
        with _lock:
            records = [
                record
                for record in _local_load()
                if record.get("service_name") == service_name
            ]
    items = [DeploymentItem(**record) for record in records]
    items.sort(key=lambda item: item.created_at, reverse=True)
    return items[:limit]


def count_for_service(service_name: str) -> int:
    collection = _firestore_collection()
    if collection is not None:
        return sum(
            1 for _ in collection.where("service_name", "==", service_name).stream()
        )
    with _lock:
        return sum(
            1 for record in _local_load() if record.get("service_name") == service_name
        )
