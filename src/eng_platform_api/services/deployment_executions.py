"""Private execution metadata; the public deployment response stays provider-neutral."""

from __future__ import annotations

from datetime import datetime, timezone
from threading import Lock
from typing import Any

from ..config import config


def _collection():
    if (
        not config.cloud_build.enabled
        or not config.cloud_build.execution_collection
        or config.mock_mode
    ):
        return None
    from google.cloud import firestore

    project = config.cloud_build.project_id or config.monitoring.gcp_project_id
    return firestore.Client(project=project or None).collection(
        config.cloud_build.execution_collection
    )


_memory: dict[str, dict[str, Any]] = {}
_memory_lock = Lock()


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
    authorization_jti: str = "",
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
        "authorization_jti": authorization_jti,
        "status": "SUBMISSION_PENDING",
        "event_sequence": 0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    collection = _collection()
    if collection is None:
        with _memory_lock:
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


def transition_provider(
    deployment_id: str, *, from_provider: str, to_provider: str, reason: str
) -> dict[str, Any]:
    changes = {
        "provider": to_provider,
        "reason": reason,
        "status": "SUBMISSION_PENDING",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    collection = _collection()
    if collection is None:
        with _memory_lock:
            current = _memory.get(deployment_id)
            if current is None:
                raise KeyError(deployment_id)
            if current.get("provider") == to_provider:
                return dict(current)
            if current.get("provider") != from_provider:
                raise ValueError("Deployment execution provider transition is invalid")
            current.update(changes)
            return dict(current)

    document = collection.document(deployment_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists:
            raise KeyError(deployment_id)
        current = snapshot.to_dict()
        if current.get("provider") == to_provider:
            return current
        if current.get("provider") != from_provider:
            raise ValueError("Deployment execution provider transition is invalid")
        txn.update(document, changes)
        current.update(changes)
        return current

    return write(transaction)


def claim_submission(deployment_id: str) -> bool:
    """Atomically grant one caller permission to submit the external build."""
    changes = {
        "status": "SUBMITTING",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    claimable = {"SUBMISSION_PENDING", "SUBMISSION_FAILED"}
    collection = _collection()
    if collection is None:
        with _memory_lock:
            current = _memory.get(deployment_id)
            if current is None:
                raise KeyError(deployment_id)
            if current.get("build_id") or current.get("status") not in claimable:
                return False
            current.update(changes)
            return True

    document = collection.document(deployment_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists:
            raise KeyError(deployment_id)
        current = snapshot.to_dict()
        if current.get("build_id") or current.get("status") not in claimable:
            return False
        txn.update(document, changes)
        return True

    return write(transaction)


def save(deployment_id: str, **changes: Any) -> dict[str, Any]:
    collection = _collection()
    changes["updated_at"] = datetime.now(timezone.utc).isoformat()
    if collection is None:
        with _memory_lock:
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
        with _memory_lock:
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


def monthly_cloud_build_minutes(month: str) -> float:
    """Completed Cloud Build deployment minutes for one UTC YYYY-MM month."""
    collection = _collection()
    if collection is None:
        with _memory_lock:
            values = [dict(value) for value in _memory.values()]
    else:
        values = [snapshot.to_dict() or {} for snapshot in collection.stream()]
    return round(
        sum(
            float(value.get("build_minutes_estimate", 0) or 0)
            for value in values
            if value.get("provider") == "cloud_build"
            and str(value.get("build_finished_at", "")).startswith(month)
        ),
        3,
    )
