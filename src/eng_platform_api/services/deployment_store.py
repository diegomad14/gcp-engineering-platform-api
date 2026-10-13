"""Minimal deployment request persistence.

GitHub remains the source of truth for execution state. This store only keeps
the platform request identity and the GitHub object references needed to
reconstruct that state after an API restart.
"""

from __future__ import annotations

import json
import os
import threading
import hashlib
import uuid
import fcntl
import tempfile
from contextvars import ContextVar
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..models import DeploymentItem

_DEFAULT_STORE_PATH = Path(
    os.getenv("ENG_PLATFORM_DEPLOYMENT_STORE_PATH", "data/deployments.json")
)
_COLLECTION = os.getenv("ENG_PLATFORM_DEPLOYMENT_FIRESTORE_COLLECTION", "")
# A single Firestore IN query supports at most 30 comparison values.
_FIRESTORE_IN_QUERY_LIMIT = 30
_lock = threading.RLock()
_command_key: ContextVar[str] = ContextVar("deployment_command_key", default="")


@contextmanager
def using_command_key(key: str):
    token = _command_key.set(key)
    try:
        yield
    finally:
        _command_key.reset(token)


def persist_before_dispatch(item: DeploymentItem) -> None:
    """Bind the durable admission to its external object before any dispatch."""
    key = _command_key.get()
    if key:
        save(item, key)


@contextmanager
def command_guard(service_name: str):
    """Serialize the check/reserve/dispatch boundary across replicas.

    A crashed Firestore owner leaves the fence closed for investigation rather
    than letting a lease timeout create a second remote build.
    """
    collection = _firestore_collection()
    if collection is None:
        lock_path = _path().with_name(
            _path().name
            + "."
            + hashlib.sha256(service_name.encode()).hexdigest()
            + ".command.lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        return
    from google.cloud.firestore import transactional
    from fastapi import HTTPException

    document = collection._client.collection(_COLLECTION + "_command_locks").document(
        hashlib.sha256(service_name.encode()).hexdigest()
    )
    owner = uuid.uuid4().hex
    transaction = collection._client.transaction()

    @transactional
    def claim(txn):
        current = document.get(transaction=txn)
        if current.exists and (current.to_dict() or {}).get("owner"):
            raise HTTPException(
                409,
                "Deployment command is pending; provider and dispatch require reconciliation",
            )
        txn.set(document, {"owner": owner, "service_name": service_name})

    claim(transaction)
    try:
        yield
    finally:
        # No TTL or force acquisition: uncertain/crashed dispatch stays fenced.
        @transactional
        def release(txn):
            snapshot = document.get(transaction=txn)
            if snapshot.exists and (snapshot.to_dict() or {}).get("owner") == owner:
                txn.delete(document)

        release(collection._client.transaction())


def _path() -> Path:
    return (
        _DEFAULT_STORE_PATH
        if _DEFAULT_STORE_PATH.is_absolute()
        else Path.cwd() / _DEFAULT_STORE_PATH
    )


def _local_load() -> list[dict[str, Any]]:
    path = _path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("Invalid deployment snapshot")
        return payload
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        from fastapi import HTTPException

        raise HTTPException(
            503, "Deployment state is unavailable; reconcile before retrying"
        ) from None


def _local_save(items: list[dict[str, Any]]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".deployments-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(items, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def _local_write_guard():
    """Protect the entire snapshot read/modify/write across services/processes."""
    with _lock:
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Lock a stable separate inode: atomic replacement changes the JSON inode.
        with path.with_name(path.name + ".store.lock").open("a") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@lru_cache(maxsize=4)
def firestore_client(project_id: str):
    from google.cloud import firestore

    return firestore.Client(project=project_id) if project_id else firestore.Client()


_firestore_client = firestore_client


def release_authorization_collection() -> str:
    return os.getenv("ENG_PLATFORM_RELEASE_AUTH_FIRESTORE_COLLECTION", "").strip()


def _firestore_collection():
    if not _COLLECTION:
        return None
    project_id = os.getenv("ENG_PLATFORM_GCP_PROJECT_ID", "").strip()
    return _firestore_client(project_id).collection(_COLLECTION)


def save(item: DeploymentItem, idempotency_key: str) -> DeploymentItem:
    collection = _firestore_collection()
    if collection is not None:
        previous_intent = (
            find_by_idempotency_key(idempotency_key) if idempotency_key else None
        )
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
        # The new binding is durable first. A failed cleanup leaves a closed
        # admission for reconciliation, never permission for another dispatch.
        if previous_intent and previous_intent.id != item.id:
            collection.document(previous_intent.id).delete()
        return item
    with _local_write_guard():
        current = _local_load()
        previous = next((r for r in current if r.get("id") == item.id), {})
        record = {
            **item.model_dump(),
            "idempotency_key": idempotency_key or previous.get("idempotency_key", ""),
        }
        records = [
            r
            for r in current
            if r.get("id") != item.id
            and (not idempotency_key or r.get("idempotency_key") != idempotency_key)
        ]
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


def idempotency_key_for(deployment_id: str) -> str:
    """Read back the original request key so a failover keeps one identity."""
    collection = _firestore_collection()
    if collection is not None:
        snapshot = collection.document(deployment_id).get()
        record = snapshot.to_dict() if snapshot.exists else None
    else:
        with _lock:
            record = next(
                (r for r in _local_load() if r.get("id") == deployment_id), None
            )
    return str((record or {}).get("idempotency_key", ""))


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
    return list_for_service_with_total(service_name, limit=limit)[0]


def list_for_service_with_total(
    service_name: str, limit: int = 20
) -> tuple[list[DeploymentItem], int]:
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
    return items[:limit], len(items)


def latest_for_services(service_names: list[str]) -> dict[str, DeploymentItem]:
    if not service_names:
        return {}
    service_names = list(dict.fromkeys(service_names))
    collection = _firestore_collection()
    if collection is not None:
        records: list[dict[str, Any]] = []
        for offset in range(0, len(service_names), _FIRESTORE_IN_QUERY_LIMIT):
            batch = service_names[offset : offset + _FIRESTORE_IN_QUERY_LIMIT]
            snapshots = collection.where("service_name", "in", batch).stream()
            records.extend(snapshot.to_dict() for snapshot in snapshots)
    else:
        with _lock:
            records = [
                record
                for record in _local_load()
                if record.get("service_name") in service_names
            ]
    items = [DeploymentItem(**record) for record in records]
    items.sort(key=lambda item: item.created_at, reverse=True)
    latest: dict[str, DeploymentItem] = {}
    for item in items:
        latest.setdefault(item.service_name, item)
    return latest


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


def list_unfinished(limit: int = 200) -> list[DeploymentItem]:
    """Return every request that still claims to be running, across services.

    The reconciliation sweep needs one pass over all services, which the
    per-service readers cannot express.  GitHub remains the source of truth for
    execution state; this only finds the candidates to re-read.
    """
    collection = _firestore_collection()
    if collection is not None:
        records = [snapshot.to_dict() for snapshot in collection.stream()]
    else:
        with _lock:
            records = list(_local_load())
    items = [
        DeploymentItem(**record)
        for record in records
        if record.get("status") not in _TERMINAL_STORE_STATUSES
    ]
    items.sort(key=lambda item: item.created_at)
    return items[:limit]


_TERMINAL_STORE_STATUSES = frozenset(
    {"SUCCEEDED", "FAILED", "ROLLED_BACK", "ROLLBACK_FAILED"}
)
