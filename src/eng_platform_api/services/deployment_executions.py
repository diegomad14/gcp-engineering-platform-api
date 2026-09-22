"""Private execution metadata; the public deployment response stays provider-neutral."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..config import config


def _collection():
    if not config.cloud_build.execution_collection or config.mock_mode:
        return None
    from google.cloud import firestore

    project = config.cloud_build.project_id or config.monitoring.gcp_project_id
    return firestore.Client(project=project or None).collection(
        config.cloud_build.execution_collection
    )


_memory: dict[str, dict[str, Any]] = {}


def get(deployment_id: str) -> dict[str, Any] | None:
    collection = _collection()
    if collection is None:
        value = _memory.get(deployment_id)
        return dict(value) if value else None
    snapshot = collection.document(deployment_id).get()
    return snapshot.to_dict() if snapshot.exists else None


def reserve(
    deployment_id: str,
    *,
    provider: str,
    fingerprint: str,
    service_name: str,
    repository: str,
    sha: str,
    tag: str,
    kind: str,
    reason: str = "",
) -> dict[str, Any]:
    """Persist intent before an external call and never overwrite its identity."""
    record = {
        "deployment_id": deployment_id,
        "provider": provider,
        "fingerprint": fingerprint,
        "service_name": service_name,
        "repository": repository,
        "sha": sha,
        "tag": tag,
        "kind": kind,
        "reason": reason,
        "status": "SUBMISSION_PENDING",
        "event_sequence": 0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    collection = _collection()
    if collection is None:
        existing = _memory.get(deployment_id)
        if existing:
            if existing["fingerprint"] != fingerprint:
                raise ValueError("Deployment execution identity cannot change")
            return dict(existing)
        _memory[deployment_id] = record
        return dict(record)
    document = collection.document(deployment_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        current = document.get(transaction=txn)
        if current.exists:
            value = current.to_dict()
            if value.get("fingerprint") != fingerprint:
                raise ValueError("Deployment execution identity cannot change")
            return value
        txn.set(document, record)
        return record

    return write(transaction)


def save(deployment_id: str, **changes: Any) -> dict[str, Any]:
    collection = _collection()
    changes["updated_at"] = datetime.now(timezone.utc).isoformat()
    if collection is None:
        if deployment_id not in _memory:
            raise KeyError(deployment_id)
        _memory[deployment_id].update(changes)
        return dict(_memory[deployment_id])
    document = collection.document(deployment_id)
    document.set(changes, merge=True)
    return document.get().to_dict()


def accept_event(
    deployment_id: str, sequence: int, **changes: Any
) -> tuple[dict[str, Any], bool]:
    """Atomically accept only a newer engine event.

    The boolean distinguishes an accepted transition from a harmless duplicate
    or out-of-order delivery, so callers never project stale state publicly.
    """
    changes["event_sequence"] = sequence
    changes["updated_at"] = datetime.now(timezone.utc).isoformat()
    collection = _collection()
    if collection is None:
        current = _memory.get(deployment_id)
        if current is None:
            raise KeyError(deployment_id)
        if sequence <= int(current.get("event_sequence", 0)):
            return dict(current), False
        current.update(changes)
        return dict(current), True

    document = collection.document(deployment_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists:
            raise KeyError(deployment_id)
        current = snapshot.to_dict()
        if sequence <= int(current.get("event_sequence", 0)):
            return current, False
        txn.update(document, changes)
        current.update(changes)
        return current, True

    return write(transaction)
