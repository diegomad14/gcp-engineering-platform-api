"""Persistent GitHub Actions billing circuit breaker.

The circuit is account-scoped because private Actions minutes and payment state
are shared by all repositories owned by that billing account.  Time never closes
the circuit: a real, started health probe is required.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from ..config import config

_memory: dict[str, dict[str, Any]] = {}
_lock = RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _id(owner: str) -> str:
    return hashlib.sha256(owner.strip().lower().encode()).hexdigest()


def _collection():
    settings = config.release_orchestrator
    if (
        config.mock_mode
        or not (settings.enabled or config.cloud_build.enabled)
        or not settings.circuit_collection
    ):
        return None
    from google.cloud import firestore

    project = config.cloud_build.project_id or config.monitoring.gcp_project_id
    return firestore.Client(project=project or None).collection(
        settings.circuit_collection
    )


def get(owner: str) -> dict[str, Any]:
    circuit_id = _id(owner)
    collection = _collection()
    if collection is None:
        with _lock:
            return dict(
                _memory.get(
                    circuit_id,
                    {"owner": owner, "state": "closed", "updated_at": ""},
                )
            )
    snapshot = collection.document(circuit_id).get()
    return (
        snapshot.to_dict()
        if snapshot.exists
        else {"owner": owner, "state": "closed", "updated_at": ""}
    )


def is_open(owner: str) -> bool:
    return get(owner).get("state") == "open"


def open_circuit(
    owner: str,
    *,
    reason: str,
    repository: str = "",
    run_id: str = "",
    evidence: str = "",
) -> tuple[dict[str, Any], bool]:
    """Open once and retain sanitized evidence explaining the decision."""
    now = _now()
    circuit_id = _id(owner)
    value = {
        "owner": owner,
        "state": "open",
        "reason": reason[:256],
        "repository": repository,
        "run_id": str(run_id),
        "evidence": evidence[:1000],
        "opened_at": now,
        "updated_at": now,
    }
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(circuit_id)
            if current and current.get("state") == "open":
                return dict(current), False
            _memory[circuit_id] = value
            return dict(value), True

    document = collection.document(circuit_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if snapshot.exists:
            current = snapshot.to_dict()
            if current.get("state") == "open":
                return current, False
        txn.set(document, value)
        return value, True

    return write(transaction)


def request_probe(
    owner: str, *, repository: str, workflow: str, requested_by: str
) -> dict[str, Any]:
    """Bind a future health result before dispatching the probe workflow."""
    circuit = get(owner)
    if circuit.get("state") != "open":
        raise ValueError("GitHub Actions circuit is not open")
    changes = {
        "probe": {
            "status": "requested",
            "nonce": secrets.token_urlsafe(24),
            "repository": repository,
            "workflow": workflow,
            "requested_by": requested_by,
            "requested_at": _now(),
        },
        "updated_at": _now(),
    }
    circuit_id = _id(owner)
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(circuit_id)
            if current is None or current.get("state") != "open":
                raise ValueError("GitHub Actions circuit is not open")
            current.update(changes)
            return dict(current)
    document = collection.document(circuit_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists or snapshot.to_dict().get("state") != "open":
            raise ValueError("GitHub Actions circuit is not open")
        txn.update(document, changes)
        current = snapshot.to_dict()
        current.update(changes)
        return current

    return write(transaction)


def record_probe(
    owner: str,
    *,
    repository: str,
    workflow: str,
    run_id: str,
    conclusion: str,
    jobs_started: int,
) -> dict[str, Any]:
    circuit = get(owner)
    requested = circuit.get("probe", {})
    if (
        circuit.get("state") != "open"
        or requested.get("status") != "requested"
        or requested.get("repository") != repository
        or requested.get("workflow") != workflow
    ):
        raise ValueError("Health probe does not match a requested open-circuit probe")
    changes = {
        "probe": {
            **requested,
            "status": "verified",
            "repository": repository,
            "workflow": workflow,
            "run_id": str(run_id),
            "conclusion": conclusion,
            "jobs_started": int(jobs_started),
            "completed_at": _now(),
        },
        "updated_at": _now(),
    }
    circuit_id = _id(owner)
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(circuit_id)
            if current is None or current.get("state") != "open":
                raise ValueError("GitHub Actions circuit is not open")
            current.update(changes)
            return dict(current)
    document = collection.document(circuit_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists:
            raise ValueError("GitHub Actions circuit does not exist")
        current = snapshot.to_dict()
        expected = current.get("probe", {})
        if (
            current.get("state") != "open"
            or expected.get("status") != "requested"
            or expected.get("repository") != repository
            or expected.get("workflow") != workflow
        ):
            raise ValueError(
                "Health probe does not match a requested open-circuit probe"
            )
        txn.update(document, changes)
        current.update(changes)
        return current

    return write(transaction)


def close_after_successful_probe(owner: str, *, run_id: str) -> dict[str, Any]:
    """Close only when the recorded probe used a runner and succeeded."""
    circuit_id = _id(owner)
    now = _now()
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(circuit_id)
            if current is None:
                raise ValueError("GitHub Actions circuit does not exist")
            probe = current.get("probe", {})
            if (
                current.get("state") != "open"
                or probe.get("status") != "verified"
                or str(probe.get("run_id")) != str(run_id)
                or probe.get("conclusion") != "success"
                or int(probe.get("jobs_started", 0)) < 1
            ):
                raise ValueError("A successful started health probe is required")
            current.update(
                {
                    "state": "closed",
                    "closed_at": now,
                    "updated_at": now,
                    "close_reason": "successful_health_probe",
                }
            )
            return dict(current)

    document = collection.document(circuit_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists:
            raise ValueError("GitHub Actions circuit does not exist")
        current = snapshot.to_dict()
        probe = current.get("probe", {})
        if (
            current.get("state") != "open"
            or probe.get("status") != "verified"
            or str(probe.get("run_id")) != str(run_id)
            or probe.get("conclusion") != "success"
            or int(probe.get("jobs_started", 0)) < 1
        ):
            raise ValueError("A successful started health probe is required")
        changes = {
            "state": "closed",
            "closed_at": now,
            "updated_at": now,
            "close_reason": "successful_health_probe",
        }
        txn.update(document, changes)
        current.update(changes)
        return current

    return write(transaction)


def claim_usage_alert(
    owner: str, *, month: str, threshold: int, observed_minutes: float
) -> bool:
    """Emit each account/month Cloud Build usage threshold exactly once."""
    marker = f"{month}:{threshold}"
    circuit_id = _id(owner)
    now = _now()
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.setdefault(
                circuit_id,
                {"owner": owner, "state": "closed", "updated_at": now},
            )
            alerts = dict(current.get("cloud_build_usage_alerts") or {})
            if marker in alerts:
                return False
            alerts[marker] = {
                "threshold": threshold,
                "observed_minutes": observed_minutes,
                "emitted_at": now,
            }
            current["cloud_build_usage_alerts"] = alerts
            current["updated_at"] = now
            return True

    document = collection.document(circuit_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        current = (
            snapshot.to_dict()
            if snapshot.exists
            else {"owner": owner, "state": "closed"}
        )
        alerts = dict(current.get("cloud_build_usage_alerts") or {})
        if marker in alerts:
            return False
        alerts[marker] = {
            "threshold": threshold,
            "observed_minutes": observed_minutes,
            "emitted_at": now,
        }
        changes = {
            "owner": owner,
            "state": current.get("state", "closed"),
            "cloud_build_usage_alerts": alerts,
            "updated_at": now,
        }
        if snapshot.exists:
            txn.update(document, changes)
        else:
            txn.set(document, changes)
        return True

    return bool(write(transaction))
