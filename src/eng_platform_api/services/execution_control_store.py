"""Shared release execution control backed by local state or Firestore.

The local file is a durable single-host development backend, not an in-memory
lock.  Production deployments must configure the existing Firestore client;
the API never treats lease expiry as proof that an external effect finished.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..models import (
    ExecutionIntent,
    ExecutionIntentCreateRequest,
    ExecutionIntentReconcileRequest,
    ExecutionIntentResultRequest,
    ExecutionLease,
    ExecutionLeaseAcquireRequest,
    ExecutionLeaseReconcileRequest,
    ExecutionLeaseReleaseRequest,
    ExecutionLeaseRenewRequest,
    ReleaseExecutionContext,
)
from ..config import config
from . import deployment_store, release_authorization_store

try:
    import fcntl
except ImportError:  # pragma: no cover - the supported local hosts are POSIX.
    fcntl = None


_DEFAULT_STORE_PATH = Path(
    os.getenv(
        "ENG_PLATFORM_RELEASE_CONTROL_STORE_PATH", "data/release_execution_control.json"
    )
)
_COLLECTION = os.getenv("ENG_PLATFORM_RELEASE_CONTROL_FIRESTORE_COLLECTION", "").strip()
_lock = threading.RLock()
_CONTEXT_FIELDS = (
    "release_id",
    "repository",
    "source_sha",
    "tag",
    "artifact_digest",
    "target",
    "operation",
    "configuration_hash",
    "actor_id",
)


class ExecutionControlError(ValueError):
    """Base error for a rejected or unavailable execution-control operation."""


class AuthorizationRequired(ExecutionControlError):
    """The platform authorization was not consumed or did not match."""


class LeaseConflict(ExecutionControlError):
    """Another live owner holds the requested shared scope."""


class StaleOwner(ExecutionControlError):
    """A client tried to mutate a lease with an old owner version."""


class LeaseTakeoverBlocked(ExecutionControlError):
    """Lease expiry or an indeterminate effect is not a takeover precondition."""


class IntentConflict(ExecutionControlError):
    """An idempotency key was reused with a different immutable payload."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionControlError("Stored execution-control time is invalid") from exc


def _context(value: Any) -> ReleaseExecutionContext:
    data = {field: getattr(value, field) for field in _CONTEXT_FIELDS}
    return ReleaseExecutionContext(**data)


def _context_dict(value: Any) -> dict[str, Any]:
    return _context(value).model_dump()


def _fingerprint(request: ExecutionIntentCreateRequest) -> str:
    payload = request.model_dump()
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _path() -> Path:
    return (
        _DEFAULT_STORE_PATH
        if _DEFAULT_STORE_PATH.is_absolute()
        else Path.cwd() / _DEFAULT_STORE_PATH
    )


def _empty_state() -> dict[str, dict[str, dict[str, Any]]]:
    return {"leases": {}, "intents": {}}


def _read_local(path: Path) -> dict[str, dict[str, dict[str, Any]]]:
    if not path.exists():
        return _empty_state()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutionControlError(
            "Shared execution-control store is unreadable"
        ) from exc
    if not isinstance(value, dict) or not all(
        isinstance(value.get(key, {}), dict) for key in ("leases", "intents")
    ):
        raise ExecutionControlError("Shared execution-control store has invalid shape")
    return {"leases": value.get("leases", {}), "intents": value.get("intents", {})}


def _write_local(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


@contextlib.contextmanager
def _file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    handle = lock_path.open("a+", encoding="utf-8")
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def _local_mutate(mutator: Callable[[dict[str, Any]], tuple[Any, bool]]) -> Any:
    path = _path()
    with _lock, _file_lock(path):
        state = _read_local(path)
        result, changed = mutator(state)
        if changed:
            _write_local(path, state)
        return result


def _project_id() -> str:
    return os.getenv("ENG_PLATFORM_GCP_PROJECT_ID", "").strip()


def _firestore_collection():
    if not _COLLECTION:
        return None
    return deployment_store.firestore_client(_project_id()).collection(_COLLECTION)


def _document(collection: Any, prefix: str, value: str):
    key = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return collection.document(f"{prefix}-{key}")


def _firestore_transaction(collection: Any, callback: Callable[[Any], Any]) -> Any:
    from google.cloud import firestore

    client = getattr(collection, "_client", None) or deployment_store.firestore_client(
        _project_id()
    )
    transaction = client.transaction()
    return firestore.transactional(callback)(transaction)


def _same_lease(current: dict[str, Any], request: ExecutionLeaseAcquireRequest) -> bool:
    return (
        current.get("scope") == request.scope
        and current.get("scope_key") == request.scope_key
        and current.get("owner_id") == request.owner_id
        and current.get("context") == _context_dict(request)
        and current.get("authorization_jti") == request.authorization_jti
    )


def _new_lease(
    request: ExecutionLeaseAcquireRequest,
    current: dict[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    return ExecutionLease(
        lease_id=str(uuid.uuid4()),
        scope=request.scope,
        scope_key=request.scope_key,
        owner_id=request.owner_id,
        generation=int((current or {}).get("generation", 0)) + 1,
        version=1,
        acquired_at=_iso(now),
        expires_at=_iso(now + timedelta(seconds=request.ttl_seconds)),
        status="HELD",
        context=_context(request),
        authorization_jti=request.authorization_jti,
        reconciliation_id="",
        reconciliation_observation="",
        takeover_allowed=False,
        final_status="",
        released_at="",
    ).model_dump()


def _acquire_in_state(
    state: dict[str, Any], request: ExecutionLeaseAcquireRequest
) -> tuple[ExecutionLease, bool]:
    current = state["leases"].get(request.scope_key)
    now = _now()
    if current:
        current_expired = _parse_time(str(current.get("expires_at", ""))) <= now
        if current.get("status") == "HELD" and not current_expired:
            if _same_lease(current, request):
                return ExecutionLease(**current), False
            raise LeaseConflict("Execution scope is held by another owner")
        can_take_over = (
            current.get("status") == "RECONCILED"
            and current.get("takeover_allowed") is True
            and request.reconciliation_id
            and request.reconciliation_id == current.get("reconciliation_id")
        )
        if current.get("status") != "RELEASED" and not can_take_over:
            raise LeaseTakeoverBlocked(
                "Lease is expired or uncertain; reconcile an external observation before takeover"
            )
    lease = _new_lease(request, current, now)
    state["leases"][request.scope_key] = lease
    return ExecutionLease(**lease), True


def _assert_owner(
    current: dict[str, Any] | None,
    request: ExecutionLeaseRenewRequest,
    *,
    allow_released: bool = False,
) -> None:
    if not current or any(
        current.get(key) != getattr(request, key)
        for key in ("lease_id", "owner_id", "generation", "version")
    ):
        raise StaleOwner("Lease owner or version is stale")
    if current.get("status") == "RELEASED" and allow_released:
        return
    if current.get("status") != "HELD":
        raise StaleOwner("Lease is no longer held by an active owner")
    if _parse_time(str(current.get("expires_at", ""))) <= _now():
        raise StaleOwner("Lease has expired; renewal is not allowed")


def _renew_in_state(
    state: dict[str, Any], request: ExecutionLeaseRenewRequest
) -> tuple[ExecutionLease, bool]:
    current = state["leases"].get(request.scope_key)
    _assert_owner(current, request)
    now = _now()
    current["version"] = int(current["version"]) + 1
    current["expires_at"] = _iso(now + timedelta(seconds=request.ttl_seconds))
    state["leases"][request.scope_key] = current
    return ExecutionLease(**current), True


def _release_in_state(
    state: dict[str, Any], request: ExecutionLeaseReleaseRequest
) -> tuple[ExecutionLease, bool]:
    current = state["leases"].get(request.scope_key)
    if (
        current
        and current.get("status") == "RELEASED"
        and current.get("final_status") == request.final_status
        and all(
            current.get(key) == getattr(request, key)
            for key in ("lease_id", "owner_id", "generation", "version")
        )
    ):
        return ExecutionLease(**current), False
    _assert_owner(current, request)
    current["status"] = "RELEASED"
    current["final_status"] = request.final_status
    current["released_at"] = _iso(_now())
    state["leases"][request.scope_key] = current
    return ExecutionLease(**current), True


def _reconcile_lease_in_state(
    state: dict[str, Any], request: ExecutionLeaseReconcileRequest
) -> tuple[ExecutionLease, bool]:
    current = state["leases"].get(request.scope_key)
    if not current or any(
        current.get(key) != getattr(request, key)
        for key in ("lease_id", "owner_id", "generation")
    ):
        raise StaleOwner("Lease owner or generation is stale")
    if current.get("status") not in {"HELD", "UNKNOWN", "RECONCILED"}:
        raise LeaseTakeoverBlocked("Released lease no longer needs reconciliation")
    current["status"] = "RECONCILED"
    current["reconciliation_id"] = request.reconciliation_id
    current["reconciliation_observation"] = request.observation
    current["takeover_allowed"] = request.observation == "NOT_STARTED"
    state["leases"][request.scope_key] = current
    return ExecutionLease(**current), True


def _assert_intent_lease(
    state: dict[str, Any], request: ExecutionIntentCreateRequest
) -> dict[str, Any]:
    lease = state["leases"].get(request.scope_key)
    if not lease or any(
        (
            lease.get("lease_id") != request.lease_id,
            lease.get("owner_id") != request.owner_id,
            lease.get("generation") != request.lease_generation,
        )
    ):
        raise StaleOwner("Intent does not belong to the current lease owner")
    if lease.get("status") != "HELD":
        raise StaleOwner("Intent cannot be created without a held lease")
    if _parse_time(str(lease.get("expires_at", ""))) <= _now():
        raise StaleOwner("Intent lease has expired")
    return lease


def _create_intent_in_state(
    state: dict[str, Any], request: ExecutionIntentCreateRequest
) -> tuple[ExecutionIntent, bool, bool]:
    key = request.idempotency_key
    fingerprint = _fingerprint(request)
    existing = state["intents"].get(key)
    if existing:
        if existing.get("intent_fingerprint") != fingerprint:
            raise IntentConflict(
                "Idempotency key already has different immutable inputs"
            )
        return ExecutionIntent(**existing), False, False
    _assert_intent_lease(state, request)
    now = _iso(_now())
    record = ExecutionIntent(
        intent_id=key,
        idempotency_key=key,
        status="INTENDED",
        created_at=now,
        updated_at=now,
        context=_context(request),
        scope=request.scope,
        scope_key=request.scope_key,
        owner_id=request.owner_id,
        lease_id=request.lease_id,
        lease_generation=request.lease_generation,
        authorization_jti=request.authorization_jti,
        effect_digest=request.effect_digest,
        intent_fingerprint=fingerprint,
        result_digest="",
        error_code="",
        reconciliation_required=False,
        reconciliation_id="",
        observation_digest="",
    ).model_dump()
    state["intents"][key] = record
    return ExecutionIntent(**record), False, True


def _record_result_in_state(
    state: dict[str, Any], request: ExecutionIntentResultRequest
) -> tuple[ExecutionIntent, bool, bool]:
    current = state["intents"].get(request.intent_id)
    if not current:
        raise ExecutionControlError("Intent was not recorded before the effect")
    if current.get("status") in {"CONFIRMED", "FAILED", "UNKNOWN"}:
        if all(
            current.get(key, "") == getattr(request, key, "")
            for key in ("status", "result_digest", "error_code")
        ):
            return ExecutionIntent(**current), False, False
        raise IntentConflict("Intent already has a different terminal result")
    lease = state["leases"].get(request.scope_key)
    if not lease or any(
        (
            lease.get("lease_id") != request.lease_id,
            lease.get("owner_id") != request.owner_id,
            lease.get("generation") != request.lease_generation,
        )
    ):
        raise StaleOwner("Result does not belong to the intent lease owner")
    if request.status != "UNKNOWN" and (
        lease.get("status") != "HELD"
        or _parse_time(str(lease.get("expires_at", ""))) <= _now()
    ):
        raise StaleOwner(
            "Lease was lost before a definitive result; record UNKNOWN and reconcile"
        )
    current["status"] = request.status
    current["result_digest"] = request.result_digest
    current["error_code"] = request.error_code
    current["updated_at"] = _iso(_now())
    current["reconciliation_required"] = request.status == "UNKNOWN"
    if request.status == "UNKNOWN":
        lease["status"] = "UNKNOWN"
        lease["final_status"] = "UNKNOWN"
        lease["takeover_allowed"] = False
        state["leases"][request.scope_key] = lease
        return ExecutionIntent(**current), True, True
    state["intents"][request.intent_id] = current
    return ExecutionIntent(**current), False, True


def _reconcile_intent_in_state(
    state: dict[str, Any], request: ExecutionIntentReconcileRequest
) -> tuple[ExecutionIntent, bool, bool]:
    current = state["intents"].get(request.intent_id)
    if not current:
        raise ExecutionControlError("Intent does not exist")
    if current.get("status") in {"CONFIRMED", "FAILED"}:
        if (
            current.get("status") == request.outcome
            and current.get("reconciliation_id") == request.reconciliation_id
        ):
            return ExecutionIntent(**current), False, False
        raise IntentConflict("Terminal intent cannot be reconciled to another result")
    current["status"] = request.outcome
    current["updated_at"] = _iso(_now())
    current["reconciliation_required"] = request.outcome == "UNKNOWN"
    current["reconciliation_id"] = request.reconciliation_id
    current["observation_digest"] = request.observation_digest
    lease = state["leases"].get(current.get("scope_key", ""))
    lease_changed = False
    if lease and request.outcome in {"CONFIRMED", "FAILED"}:
        lease["status"] = "RELEASED"
        lease["final_status"] = request.outcome
        lease["released_at"] = _iso(_now())
        state["leases"][current["scope_key"]] = lease
        lease_changed = True
    if lease and request.outcome == "UNKNOWN":
        lease["status"] = "UNKNOWN"
        lease["takeover_allowed"] = False
        state["leases"][current["scope_key"]] = lease
        lease_changed = True
    state["intents"][request.intent_id] = current
    return ExecutionIntent(**current), lease_changed, True


def _firestore_lease_mutate(
    request: Any,
    mutator: Callable[[dict[str, Any]], tuple[Any, bool]],
) -> Any:
    collection = _firestore_collection()
    if collection is None:
        return None
    document = _document(collection, "lease", request.scope_key)

    def callback(transaction: Any):
        snapshot = document.get(transaction=transaction)
        current = snapshot.to_dict() if getattr(snapshot, "exists", False) else None
        state = {"leases": {request.scope_key: current}, "intents": {}}
        result, changed = mutator(state)
        if changed:
            transaction.set(document, state["leases"][request.scope_key])
        return result

    return _firestore_transaction(collection, callback)


def _firestore_intent_mutate(
    request: Any,
    mutator: Callable[[dict[str, Any]], tuple[Any, bool, bool]],
) -> Any:
    collection = _firestore_collection()
    if collection is None:
        return None
    intent_key = getattr(request, "intent_id", getattr(request, "idempotency_key", ""))
    intent_document = _document(collection, "intent", intent_key)

    def callback(transaction: Any):
        intent_snapshot = intent_document.get(transaction=transaction)
        existing_intent = (
            intent_snapshot.to_dict()
            if getattr(intent_snapshot, "exists", False)
            else None
        )
        scope_key = getattr(request, "scope_key", "") or str(
            (existing_intent or {}).get("scope_key", "")
        )
        lease_document = _document(collection, "lease", scope_key)
        lease_snapshot = lease_document.get(transaction=transaction)
        state = {
            "leases": {
                scope_key: (
                    lease_snapshot.to_dict()
                    if getattr(lease_snapshot, "exists", False)
                    else None
                )
            },
            "intents": {intent_key: existing_intent},
        }
        state["intents"] = {
            key: value for key, value in state["intents"].items() if value is not None
        }
        result, lease_changed, intent_changed = mutator(state)
        if lease_changed:
            transaction.set(lease_document, state["leases"][scope_key])
        if intent_changed:
            transaction.set(intent_document, state["intents"][intent_key])
        return result

    return _firestore_transaction(collection, callback)


def _require_authorization(request: Any) -> None:
    record = release_authorization_store.get(request.authorization_jti)
    if not record:
        raise AuthorizationRequired("Platform authorization was not consumed")
    if record.get("authorization_mode") != "local-cli":
        raise AuthorizationRequired("Authorization is not for local release execution")
    if not isinstance(record.get("exp"), int) or record["exp"] < int(time.time()):
        raise AuthorizationRequired("Platform authorization is stale")
    expected = {
        "release_id": request.release_id,
        "repository": request.repository,
        "source_sha": request.source_sha,
        "tag": request.tag,
        "artifact_digest": request.artifact_digest,
        "target": request.target,
        "operation": request.operation,
        "configuration_hash": request.configuration_hash,
        "actor_id": request.actor_id,
    }
    for key, value in expected.items():
        stored = record.get(
            key, record.get("requested_by", "") if key == "actor_id" else ""
        )
        if str(stored) != str(value):
            raise AuthorizationRequired(f"Platform authorization mismatch: {key}")
    binding = {
        "scope": request.scope,
        "scope_key": request.scope_key,
        "context": {
            key: expected[key]
            for key in (
                "release_id",
                "repository",
                "source_sha",
                "tag",
                "artifact_digest",
                "target",
                "operation",
                "configuration_hash",
            )
        },
        "effect_digest": getattr(request, "effect_digest", ""),
    }
    if not release_authorization_store.bind(
        request.authorization_jti,
        binding,
        require_durable=not config.mock_mode,
    ):
        raise AuthorizationRequired(
            "Platform authorization was reused for another effect"
        )


def acquire_lease(request: ExecutionLeaseAcquireRequest) -> ExecutionLease:
    _require_authorization(request)
    result = _firestore_lease_mutate(
        request, lambda state: _acquire_in_state(state, request)
    )
    if result is not None:
        return result
    return _local_mutate(lambda state: _acquire_in_state(state, request))


def renew_lease(request: ExecutionLeaseRenewRequest) -> ExecutionLease:
    result = _firestore_lease_mutate(
        request, lambda state: _renew_in_state(state, request)
    )
    if result is not None:
        return result
    return _local_mutate(lambda state: _renew_in_state(state, request))


def release_lease(request: ExecutionLeaseReleaseRequest) -> ExecutionLease:
    result = _firestore_lease_mutate(
        request, lambda state: _release_in_state(state, request)
    )
    if result is not None:
        return result
    return _local_mutate(lambda state: _release_in_state(state, request))


def reconcile_lease(request: ExecutionLeaseReconcileRequest) -> ExecutionLease:
    result = _firestore_lease_mutate(
        request, lambda state: _reconcile_lease_in_state(state, request)
    )
    if result is not None:
        return result
    return _local_mutate(lambda state: _reconcile_lease_in_state(state, request))


def create_intent(request: ExecutionIntentCreateRequest) -> ExecutionIntent:
    existing = get_intent(request.idempotency_key)
    if existing:
        if existing.intent_fingerprint != _fingerprint(request):
            raise IntentConflict(
                "Idempotency key already has different immutable inputs"
            )
        return existing
    _require_authorization(request)
    result = _firestore_intent_mutate(
        request, lambda state: _create_intent_in_state(state, request)
    )
    if result is not None:
        return result
    return _local_mutate(
        lambda state: (
            (result := _create_intent_in_state(state, request))[0],
            result[1] or result[2],
        )
    )


def record_intent_result(request: ExecutionIntentResultRequest) -> ExecutionIntent:
    result = _firestore_intent_mutate(
        request, lambda state: _record_result_in_state(state, request)
    )
    if result is not None:
        return result
    return _local_mutate(
        lambda state: (
            (result := _record_result_in_state(state, request))[0],
            result[1] or result[2],
        )
    )


def reconcile_intent(request: ExecutionIntentReconcileRequest) -> ExecutionIntent:
    result = _firestore_intent_mutate(
        request, lambda state: _reconcile_intent_in_state(state, request)
    )
    if result is not None:
        return result
    return _local_mutate(
        lambda state: (
            (result := _reconcile_intent_in_state(state, request))[0],
            result[1] or result[2],
        )
    )


def get_lease(scope_key: str) -> ExecutionLease | None:
    collection = _firestore_collection()
    if collection is not None:
        snapshot = _document(collection, "lease", scope_key).get()
        if not getattr(snapshot, "exists", False):
            return None
        return ExecutionLease(**(snapshot.to_dict() or {}))
    record = _local_mutate(lambda state: (state["leases"].get(scope_key), False))
    return ExecutionLease(**record) if record else None


def get_intent(intent_id: str) -> ExecutionIntent | None:
    collection = _firestore_collection()
    if collection is not None:
        snapshot = _document(collection, "intent", intent_id).get()
        if not getattr(snapshot, "exists", False):
            return None
        return ExecutionIntent(**(snapshot.to_dict() or {}))
    record = _local_mutate(lambda state: (state["intents"].get(intent_id), False))
    return ExecutionIntent(**record) if record else None
