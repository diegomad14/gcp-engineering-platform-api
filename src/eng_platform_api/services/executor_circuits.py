"""Persistent GitHub Actions billing circuit breaker.

The circuit is account-scoped because private Actions minutes and payment state
are shared by all repositories owned by that billing account.  Time never closes
the circuit: a real, started health probe is required.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import secrets
import time
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from ..config import config

_memory: dict[str, dict[str, Any]] = {}
_lock = RLock()
# Repository variables are best-effort hints; the persisted circuit remains the
# provider safety boundary. These shared intervals bound GitHub fan-out and allow
# repair after partial failures, worker crashes, or later catalog changes.
_MODE_PROPAGATION_LEASE_SECONDS = 300
_MODE_PROPAGATION_RETRY_SECONDS = 60
_MODE_PROPAGATION_REFRESH_SECONDS = 600
# Never evict a key and silently permit a previously dispatched request to run again.
_PROBE_REQUEST_LIMIT = 128


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


class CircuitRecoveryRequired(ValueError):
    """A historical non-rejection circuit cannot authorize automatic fallback."""


def require_billing_rejection(circuit: dict[str, Any]) -> None:
    """Preserve legacy circuits, but require verified billing before using them.

    Do not silently reset a historical usage/timeout circuit: repository hints
    may still suppress GitHub jobs. An explicit successful health probe repairs
    both hints and state through the existing recovery procedure.
    """
    if circuit.get("state") == "open" and circuit.get("reason") != (
        "github_actions_billing_rejection"
    ):
        raise CircuitRecoveryRequired(
            "GitHub Actions circuit lacks a verified billing rejection; "
            "explicit verified health recovery is required before automatic "
            "execution can resume"
        )


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
            value["probe_requests"] = deepcopy(
                (current or {}).get("probe_requests", {})
            )
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
        if snapshot.exists:
            value["probe_requests"] = snapshot.to_dict().get("probe_requests", {})
        txn.set(document, value)
        return value, True

    return write(transaction)


def mode_propagation_due(circuit: dict[str, Any]) -> bool:
    """Check a read snapshot before attempting the shared propagation claim."""
    if circuit.get("state") != "open":
        return False
    propagation = circuit.get("mode_propagation")
    if not isinstance(propagation, dict):
        return True
    try:
        retry_after = float(propagation.get("retry_after", 0))
    except (TypeError, ValueError, OverflowError):
        return True
    now = time.time()
    # Malformed metadata must neither affect provider safety nor suppress repair
    # forever. Allow one lease of clock skew beyond the longest valid cooldown.
    latest = now + _MODE_PROPAGATION_REFRESH_SECONDS + _MODE_PROPAGATION_LEASE_SECONDS
    return not math.isfinite(retry_after) or not now < retry_after <= latest


def claim_mode_propagation(owner: str) -> str | None:
    """Lease one best-effort sweep across processes, without changing safety state.

    No writes occur for a closed circuit or during the lease/cooldown. A crashed
    worker's claim expires; duplicate GitHub mode writes after expiry are safe.
    """
    token = secrets.token_urlsafe(24)

    def changes(current):
        if not mode_propagation_due(current):
            return None
        return {
            "mode_propagation": {
                "token": token,
                "retry_after": time.time() + _MODE_PROPAGATION_LEASE_SECONDS,
            }
        }

    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(_id(owner), {})
            update = changes(current)
            if update is None:
                return None
            current.update(update)
            return token

    document = collection.document(_id(owner))
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        update = changes(snapshot.to_dict() if snapshot.exists else {})
        if update is None:
            return None
        txn.update(document, update)
        return token

    return write(transaction)


def finish_mode_propagation(owner: str, *, token: str, succeeded: bool) -> None:
    """Release only this sweep's lease and retain a bounded retry/refresh time."""
    interval = (
        _MODE_PROPAGATION_REFRESH_SECONDS
        if succeeded
        else _MODE_PROPAGATION_RETRY_SECONDS
    )

    def changes(current):
        propagation = current.get("mode_propagation")
        if (
            current.get("state") != "open"
            or not isinstance(propagation, dict)
            or propagation.get("token") != token
        ):
            return None
        return {
            "mode_propagation": {
                "token": "",
                "retry_after": time.time() + interval,
            }
        }

    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(_id(owner), {})
            update = changes(current)
            if update is not None:
                current.update(update)
        return

    document = collection.document(_id(owner))
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        update = changes(snapshot.to_dict() if snapshot.exists else {})
        if update is not None:
            txn.update(document, update)

    write(transaction)


def public_probe_status(circuit: dict[str, Any]) -> dict[str, Any]:
    """Explicit allowlist: circuit evidence, nonces and request hashes stay private."""
    probe = circuit.get("probe", {})
    return {
        "accepted": True,
        "state": circuit.get("state"),
        **{
            key: probe[key]
            for key in (
                "repository",
                "workflow",
                "status",
                "dispatch_status",
                "requested_at",
                "completed_at",
                "run_id",
                "conclusion",
                "jobs_started",
            )
            if key in probe
        },
    }


def _probe_history(current: dict[str, Any]) -> dict[str, Any]:
    history = deepcopy(current.get("probe_requests", {}))
    probe = current.get("probe", {})
    key = probe.get("request_key")
    if key and key in history:
        result = public_probe_status(current)
        history[key]["circuit"] = {
            "state": result.pop("state"),
            "probe": {
                name: value for name, value in result.items() if name != "accepted"
            },
        }
    return history


class ProbeRequestConflict(ValueError):
    """A controlled validation/conflict message safe for public probe clients."""


def reserve_probe(
    owner: str,
    *,
    repository: str,
    workflow: str,
    requested_by: str,
    reason: str,
    idempotency_key: str,
) -> tuple[dict[str, Any], bool]:
    """Reserve once in the circuit transaction, before any external dispatch.

    Pending (including uncertain) requests never expire or get overwritten.
    A bounded durable history also prevents replay after callback/reopening.
    """
    if not reason.strip() or len(reason) > 500:
        raise ProbeRequestConflict("reason must contain 1 to 500 characters")
    if not idempotency_key.strip() or len(idempotency_key) > 128:
        raise ProbeRequestConflict("idempotency_key must contain 1 to 128 characters")
    key = hashlib.sha256(idempotency_key.encode()).hexdigest()
    fingerprint = hashlib.sha256(
        json.dumps(
            [repository, workflow, requested_by.lower(), reason],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    probe = {
        "status": "requested",
        "dispatch_status": "reserved",
        "nonce": secrets.token_urlsafe(24),
        "repository": repository,
        "workflow": workflow,
        "requested_by": requested_by,
        "requested_at": _now(),
        "request_key": key,
    }

    def reserve(current):
        history = deepcopy(current.get("probe_requests", {}))
        if key in history:
            previous = history[key]
            if previous.get("fingerprint") != fingerprint:
                raise ProbeRequestConflict(
                    "idempotency_key is already bound to another request"
                )
            return deepcopy(previous["circuit"]), False
        if current.get("state") != "open":
            raise ProbeRequestConflict("GitHub Actions circuit is not open")
        if current.get("probe", {}).get("status") == "requested":
            raise ProbeRequestConflict(
                "A health probe is already pending; verify its outcome first"
            )
        if len(history) >= _PROBE_REQUEST_LIMIT:
            raise ProbeRequestConflict(
                "Health probe request history is full; operator review is required"
            )
        current.update({"probe": probe, "updated_at": _now()})
        history[key] = {"fingerprint": fingerprint}
        current["probe_requests"] = history
        current["probe_requests"] = _probe_history(current)
        return current, True

    collection = _collection()
    if collection is None:
        if not config.mock_mode:
            raise RuntimeError(
                "Persistent circuit storage is required for health probes"
            )
        with _lock:
            current = deepcopy(_memory.get(_id(owner), {}))
            result, created = reserve(current)
            if created:
                _memory[_id(owner)] = current
            return deepcopy(result), created
    document = collection.document(_id(owner))
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        current, created = reserve(snapshot.to_dict() if snapshot.exists else {})
        if created:
            txn.update(
                document,
                {
                    name: current[name]
                    for name in ("probe", "probe_requests", "updated_at")
                },
            )
        return current, created

    return write(transaction)


def request_probe(
    owner: str,
    *,
    repository: str,
    workflow: str,
    requested_by: str,
) -> dict[str, Any]:
    """Compatibility for internal callers; pending probes cannot be replaced."""
    circuit, _ = reserve_probe(
        owner,
        repository=repository,
        workflow=workflow,
        requested_by=requested_by,
        reason="Explicit health probe",
        idempotency_key=secrets.token_hex(24),
    )
    return circuit


def finish_probe_dispatch(
    owner: str,
    *,
    nonce: str,
    dispatch_status: str,
) -> dict[str, Any]:
    """Persist delivery outcome without overwriting a concurrent verified callback."""
    if dispatch_status not in {"dispatched", "uncertain", "not_dispatched"}:
        raise ValueError("Invalid health probe dispatch status")

    def changes(current):
        probe = current.get("probe", {})
        if probe.get("nonce") != nonce:
            raise ValueError("Health probe reservation changed")
        current["probe"] = {**probe, "dispatch_status": dispatch_status}
        if dispatch_status == "not_dispatched" and probe.get("status") == "requested":
            current["probe"]["status"] = "cancelled"
        current["updated_at"] = _now()
        current["probe_requests"] = _probe_history(current)
        return {
            name: current[name] for name in ("probe", "probe_requests", "updated_at")
        }

    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(_id(owner), {})
            current.update(changes(deepcopy(current)))
            return deepcopy(current)
    document = collection.document(_id(owner))
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        current = snapshot.to_dict() if snapshot.exists else {}
        update = changes(current)
        txn.update(document, update)
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
    nonce: str | None = None,
) -> dict[str, Any]:
    circuit = get(owner)
    requested = circuit.get("probe", {})
    if (
        circuit.get("state") != "open"
        or requested.get("status") != "requested"
        or requested.get("repository") != repository
        or requested.get("workflow") != workflow
        or (nonce is not None and requested.get("nonce") != nonce)
    ):
        raise ValueError("Health probe does not match a requested open-circuit probe")
    changes: dict[str, Any] = {
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
            expected = current.get("probe", {})
            if (
                expected.get("status") != "requested"
                or expected.get("repository") != repository
                or expected.get("workflow") != workflow
                or expected.get("nonce") != requested.get("nonce")
            ):
                raise ValueError(
                    "Health probe does not match a requested open-circuit probe"
                )
            changes["probe"]["dispatch_status"] = expected.get(
                "dispatch_status", "reserved"
            )
            current.update(changes)
            current["probe_requests"] = _probe_history(current)
            return deepcopy(current)
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
            or expected.get("nonce") != requested.get("nonce")
        ):
            raise ValueError(
                "Health probe does not match a requested open-circuit probe"
            )
        changes["probe"]["dispatch_status"] = expected.get(
            "dispatch_status", "reserved"
        )
        current.update(changes)
        changes["probe_requests"] = _probe_history(current)
        current.update(changes)
        txn.update(document, changes)
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
            current["probe_requests"] = _probe_history(current)
            return deepcopy(current)

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
        current.update(changes)
        changes["probe_requests"] = _probe_history(current)
        current.update(changes)
        txn.update(document, changes)
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
