"""Private, transactional build ledger and collector checkpoints."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from functools import lru_cache
from threading import RLock
from typing import Any, Callable

from ..config import config

COLLECTION = "cloud_build_usage"
_memory: dict[str, dict[str, Any]] = {}
_lock = RLock()


@lru_cache(maxsize=4)
def _client(project: str):
    from google.cloud import firestore

    return firestore.Client(project=project)


def _collection():
    if config.mock_mode:
        return None
    return _client(config.cloud_build.project_id).collection(COLLECTION)


def get(key: str) -> dict[str, Any] | None:
    collection = _collection()
    if collection is None:
        with _lock:
            return deepcopy(_memory.get(key))
    snapshot = collection.document(key).get()
    return snapshot.to_dict() if snapshot.exists else None


def execution(category: str, execution_id: str) -> dict[str, Any] | None:
    """Read identity only; reuse the channel and never update functional state."""
    if config.mock_mode:
        return None
    name = (
        config.cloud_build.execution_collection
        if category == "deployment"
        else config.release_orchestrator.execution_collection
    )
    snapshot = (
        _client(config.cloud_build.project_id)
        .collection(name)
        .document(execution_id)
        .get(timeout=30)
    )
    return snapshot.to_dict() if snapshot.exists else None


def update(
    key: str, transform: Callable[[dict[str, Any]], dict[str, Any]]
) -> dict[str, Any]:
    """Atomic read/modify/write; transforms must have no external side effects."""
    collection = _collection()
    if collection is None:
        with _lock:
            value = transform(deepcopy(_memory.get(key, {})))
            _memory[key] = deepcopy(value)
            return deepcopy(value)
    from google.cloud.firestore import transactional

    document = collection.document(key)

    @transactional
    def write(transaction):
        snapshot = document.get(transaction=transaction)
        value = transform(snapshot.to_dict() or {})
        transaction.set(document, value)
        return value

    return write(document._client.transaction())


def rows() -> list[dict[str, Any]]:
    collection = _collection()
    if collection is None:
        with _lock:
            return [
                deepcopy(row) for row in _memory.values() if row.get("kind") == "build"
            ]
    from google.cloud.firestore_v1.base_query import FieldFilter

    return [
        snapshot.to_dict()
        for snapshot in collection.where(
            filter=FieldFilter("kind", "==", "build")
        ).stream()
    ]


def claim(key: str, now: datetime, interval_seconds: int) -> bool:
    """Durable throttling; failures also wait until the next scheduled interval."""
    from uuid import uuid4

    token = uuid4().hex

    def reserve(current):
        if float(current.get("next_due", 0)) > now.timestamp():
            return current
        return {
            **current,
            "claim": token,
            "next_due": now.timestamp() + interval_seconds,
        }

    return update(key, reserve).get("claim") == token


def put_build(row: dict[str, Any]) -> dict[str, Any]:
    def merge(current):
        if current.get("complete") and not row.get("complete"):
            return current
        if current.get("observed_at", "") > row["observed_at"]:
            return current
        # Never erase a verified link because its execution record is unavailable.
        if (
            current.get("category") != "other"
            and current.get("execution_id")
            and row["category"] == "other"
        ):
            row.update(
                category=current["category"], execution_id=current["execution_id"]
            )
        return row

    return update(row["key"], merge)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
