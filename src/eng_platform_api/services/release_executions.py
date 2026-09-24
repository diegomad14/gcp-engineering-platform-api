"""Durable, idempotent state for PR quality and main release preparation."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Literal

from ..config import config
from .repository_identity import aliases

ReleaseOperation = Literal["pr_quality", "main_release"]
ReleaseProvider = Literal["github_actions", "cloud_build"]

_TRANSITIONS: dict[str, set[str]] = {
    "received": {"waiting_github", "submission_pending", "failed", "unknown"},
    "waiting_github": {
        "running_quality",
        "quality_passed",
        "quality_failed",
        "submission_pending",
        "failed",
        "unknown",
    },
    "submission_pending": {
        "submitting",
        "running_quality",
        "quality_passed",
        "quality_failed",
        "failed",
        "unknown",
    },
    "submitting": {
        "submission_pending",
        "running_quality",
        "quality_passed",
        "quality_failed",
        "failed",
        "unknown",
    },
    "running_quality": {"quality_passed", "quality_failed", "failed", "unknown"},
    "quality_passed": {
        "release_planned",
        "no_release",
        "released",
        "failed",
        "unknown",
    },
    "quality_failed": set(),
    "release_planned": {"publish_pending", "failed", "unknown"},
    "publish_pending": {"released", "release_planned", "failed", "unknown"},
    "no_release": set(),
    "released": set(),
    "failed": set(),
    "unknown": {
        "submission_pending",
        "running_quality",
        "release_planned",
        "released",
        "failed",
    },
}

_memory: dict[str, dict[str, Any]] = {}
_lock = RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def planner_retry_candidate(value: dict[str, Any]) -> bool:
    """Whether one planner-only recovery is still available for this execution."""
    return bool(
        value.get("status") == "failed"
        and value.get("provider") == "cloud_build"
        and value.get("operation") == "main_release"
        and value.get("release_engine_failed")
        and value.get("engine_event_status") == "quality_passed"
        and value.get("evidence_committed")
        and value.get("report_hash")
        and not value.get("planner_retry_checked")
        and int(value.get("planner_retry_count", 0) or 0) == 0
        and value.get("build_id")
    )


def fingerprint(
    *,
    repository: str,
    service_name: str,
    operation: ReleaseOperation,
    head_sha: str,
    base_sha: str,
    profile_hash: str,
    executor_digest: str,
    policy_hash: str,
    planner_hash: str = "",
) -> str:
    """Hash every input that can change release evidence or side effects."""
    value = {
        "repository": repository.lower(),
        "service_name": service_name,
        "operation": operation,
        "head_sha": head_sha.lower(),
        "base_sha": base_sha.lower(),
        "profile_hash": profile_hash,
        "executor_digest": executor_digest,
        "policy_hash": policy_hash,
        "planner_hash": planner_hash if operation == "main_release" else "",
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _collection():
    settings = config.release_orchestrator
    if config.mock_mode or not settings.enabled or not settings.execution_collection:
        return None
    from google.cloud import firestore

    project = config.cloud_build.project_id or config.monitoring.gcp_project_id
    return firestore.Client(project=project or None).collection(
        settings.execution_collection
    )


def get(execution_id: str) -> dict[str, Any] | None:
    collection = _collection()
    if collection is None:
        with _lock:
            value = _memory.get(execution_id)
            return dict(value) if value else None
    snapshot = collection.document(execution_id).get()
    return snapshot.to_dict() if snapshot.exists else None


def reserve(
    *,
    fingerprint_value: str,
    repository: str,
    service_name: str,
    operation: ReleaseOperation,
    head_sha: str,
    base_sha: str,
    branch: str,
    profile_hash: str,
    executor_digest: str,
    policy_hash: str,
    planner_hash: str = "",
    provider: ReleaseProvider = "github_actions",
    delivery_id: str = "",
) -> tuple[dict[str, Any], bool]:
    """Reserve intent before dispatch; the fingerprint is also the stable ID."""
    expected_fingerprint = fingerprint(
        repository=repository,
        service_name=service_name,
        operation=operation,
        head_sha=head_sha,
        base_sha=base_sha,
        profile_hash=profile_hash,
        executor_digest=executor_digest,
        policy_hash=policy_hash,
        planner_hash=planner_hash,
    )
    if fingerprint_value != expected_fingerprint:
        raise ValueError("Release execution fingerprint does not match identity")
    execution_id = fingerprint_value
    now = _now()
    value = {
        "execution_id": execution_id,
        "fingerprint": fingerprint_value,
        "repository": repository,
        "service_name": service_name,
        "operation": operation,
        "head_sha": head_sha.lower(),
        "base_sha": base_sha.lower(),
        "branch": branch,
        "profile_hash": profile_hash,
        "executor_digest": executor_digest,
        "policy_hash": policy_hash,
        "planner_hash": planner_hash,
        "provider": provider,
        "status": "waiting_github"
        if provider == "github_actions"
        else "submission_pending",
        "delivery_id": delivery_id,
        "event_sequence": 0,
        "created_at": now,
        "updated_at": now,
    }
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(execution_id)
            if current:
                if any(
                    current.get(field) != value[field]
                    for field in (
                        "fingerprint",
                        "repository",
                        "service_name",
                        "operation",
                        "head_sha",
                        "base_sha",
                        "profile_hash",
                        "executor_digest",
                        "policy_hash",
                        "planner_hash",
                    )
                ):
                    raise ValueError("Release execution identity cannot change")
                return dict(current), False
            _memory[execution_id] = value
            return dict(value), True

    document = collection.document(execution_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if snapshot.exists:
            current = snapshot.to_dict()
            if any(
                current.get(field) != value[field]
                for field in (
                    "fingerprint",
                    "repository",
                    "service_name",
                    "operation",
                    "head_sha",
                    "base_sha",
                    "profile_hash",
                    "executor_digest",
                    "policy_hash",
                    "planner_hash",
                )
            ):
                raise ValueError("Release execution identity cannot change")
            return current, False
        txn.set(document, value)
        return value, True

    return write(transaction)


def save(execution_id: str, **changes: Any) -> dict[str, Any]:
    changes["updated_at"] = _now()
    immutable = {
        "execution_id",
        "fingerprint",
        "repository",
        "service_name",
        "operation",
        "head_sha",
        "base_sha",
        "profile_hash",
        "executor_digest",
        "policy_hash",
        "planner_hash",
    }
    if immutable.intersection(changes):
        raise ValueError("Release execution identity fields are immutable")
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(execution_id)
            if current is None:
                raise KeyError(execution_id)
            _validate_status_change(current, changes)
            current.update(changes)
            return dict(current)
    document = collection.document(execution_id)
    snapshot = document.get()
    if not snapshot.exists:
        raise KeyError(execution_id)
    _validate_status_change(snapshot.to_dict(), changes)
    document.set(changes, merge=True)
    return document.get().to_dict()


def transition_to_cloud_build(execution_id: str, *, reason: str) -> tuple[dict, bool]:
    """Perform the only supported provider handoff exactly once."""
    changes = {
        "provider": "cloud_build",
        "status": "submission_pending",
        "fallback_reason": reason,
        "updated_at": _now(),
    }
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(execution_id)
            if current is None:
                raise KeyError(execution_id)
            if current.get("provider") == "cloud_build":
                return dict(current), False
            if current.get("provider") != "github_actions":
                raise ValueError("Release execution provider transition is invalid")
            if current.get("status") not in {"received", "waiting_github"}:
                raise ValueError("Release execution is no longer fallback eligible")
            current.update(changes)
            return dict(current), True

    document = collection.document(execution_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists:
            raise KeyError(execution_id)
        current = snapshot.to_dict()
        if current.get("provider") == "cloud_build":
            return current, False
        if current.get("provider") != "github_actions":
            raise ValueError("Release execution provider transition is invalid")
        if current.get("status") not in {"received", "waiting_github"}:
            raise ValueError("Release execution is no longer fallback eligible")
        txn.update(document, changes)
        current.update(changes)
        return current, True

    return write(transaction)


def claim_submission(execution_id: str) -> bool:
    """Grant one process permission to call Cloud Build create."""
    changes = {"status": "submitting", "updated_at": _now()}
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(execution_id)
            if current is None:
                raise KeyError(execution_id)
            if (
                current.get("provider") != "cloud_build"
                or current.get("build_id")
                or current.get("status")
                not in {"submission_pending", "submission_failed"}
            ):
                return False
            current.update(changes)
            return True

    document = collection.document(execution_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists:
            raise KeyError(execution_id)
        current = snapshot.to_dict()
        if (
            current.get("provider") != "cloud_build"
            or current.get("build_id")
            or current.get("status") not in {"submission_pending", "submission_failed"}
        ):
            return False
        txn.update(document, changes)
        return True

    return write(transaction)


def bind_build(execution_id: str, *, build_id: str, **metadata: Any) -> dict[str, Any]:
    """Atomically bind the single Cloud Build allowed for an execution."""
    if not build_id or len(build_id) > 128:
        raise ValueError("Cloud Build ID is invalid")
    forbidden = {
        "execution_id",
        "fingerprint",
        "repository",
        "service_name",
        "operation",
        "head_sha",
        "base_sha",
        "profile_hash",
        "executor_digest",
        "policy_hash",
        "planner_hash",
        "provider",
        "build_id",
        "provider_run_id",
    }
    if forbidden.intersection(metadata):
        raise ValueError("Cloud Build binding metadata contains reserved fields")
    changes = {
        **metadata,
        "build_id": build_id,
        "provider_run_id": build_id,
        "status": "submission_pending",
        "updated_at": _now(),
    }
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(execution_id)
            if current is None:
                raise KeyError(execution_id)
            if current.get("provider") != "cloud_build":
                raise ValueError("Release execution is not Cloud Build managed")
            current_build = str(current.get("build_id", ""))
            if current_build and current_build != build_id:
                raise ValueError("Release execution is already bound to another build")
            if current_build == build_id:
                return dict(current)
            _validate_status_change(current, changes)
            current.update(changes)
            return dict(current)

    document = collection.document(execution_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists:
            raise KeyError(execution_id)
        current = snapshot.to_dict()
        if current.get("provider") != "cloud_build":
            raise ValueError("Release execution is not Cloud Build managed")
        current_build = str(current.get("build_id", ""))
        if current_build and current_build != build_id:
            raise ValueError("Release execution is already bound to another build")
        if current_build == build_id:
            return current
        _validate_status_change(current, changes)
        txn.update(document, changes)
        current.update(changes)
        return current

    return write(transaction)


def claim_publish(execution_id: str) -> bool:
    """Grant one reconciler the right to create a tag/release."""
    changes = {"status": "publish_pending", "updated_at": _now()}
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(execution_id)
            if current is None:
                raise KeyError(execution_id)
            if (
                current.get("operation") != "main_release"
                or current.get("status") != "release_planned"
                or current.get("publish_claimed_at")
            ):
                return False
            changes["publish_claimed_at"] = _now()
            current.update(changes)
            return True

    document = collection.document(execution_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists:
            raise KeyError(execution_id)
        current = snapshot.to_dict()
        if (
            current.get("operation") != "main_release"
            or current.get("status") != "release_planned"
            or current.get("publish_claimed_at")
        ):
            return False
        changes["publish_claimed_at"] = _now()
        txn.update(document, changes)
        return True

    return write(transaction)


def _claim_build_scoped_token(
    current: dict[str, Any], *, provider_run_id: str, token_field: str, issued_field: str
) -> bool:
    provider = current.get("provider")
    if not provider_run_id:
        return False
    if provider == "cloud_build":
        if current.get("build_id") != provider_run_id:
            return False
        previous = set(current.get("previous_build_ids") or [])
    elif provider == "github_actions" and token_field == "event_token_provider_run_id":
        if str(current.get("provider_run_id", "")) != provider_run_id:
            return False
        previous = set()
    else:
        return False
    issued_for = str(current.get(token_field, ""))
    if issued_for == provider_run_id:
        return False
    if issued_for and issued_for not in previous:
        return False
    if not issued_for and current.get(issued_field) and (
        not previous or provider_run_id in previous
    ):
        # Backward compatibility for executions that recorded only the
        # one-time timestamp before token claims were scoped to build IDs.
        return False
    return True


def claim_source_token(execution_id: str, *, provider_run_id: str) -> bool:
    """Allow one repository-scoped read token per verified Cloud Build attempt."""
    now = _now()
    changes = {
        "source_token_issued_at": now,
        "source_token_build_id": provider_run_id,
        "updated_at": now,
    }
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(execution_id)
            if current is None or not _claim_build_scoped_token(
                current,
                provider_run_id=provider_run_id,
                token_field="source_token_build_id",
                issued_field="source_token_issued_at",
            ):
                return False
            current.update(changes)
            return True
    document = collection.document(execution_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists:
            return False
        current = snapshot.to_dict()
        if not _claim_build_scoped_token(
            current,
            provider_run_id=provider_run_id,
            token_field="source_token_build_id",
            issued_field="source_token_issued_at",
        ):
            return False
        txn.update(document, changes)
        return True

    return write(transaction)


def claim_event_token(
    execution_id: str, *, provider_run_id: str, token_hash: str
) -> bool:
    """Bind one callback token hash to a single build attempt."""
    if len(token_hash) != 64 or any(
        character not in "0123456789abcdef" for character in token_hash
    ):
        raise ValueError("Event token hash must be a SHA-256 hex digest")
    now = _now()
    changes = {
        "event_token_hash": token_hash,
        "event_token_issued_at": now,
        "updated_at": now,
    }
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(execution_id)
            token_field = (
                "event_token_build_id"
                if current and current.get("provider") == "cloud_build"
                else "event_token_provider_run_id"
            )
            if current is None or not _claim_build_scoped_token(
                current,
                provider_run_id=provider_run_id,
                token_field=token_field,
                issued_field="event_token_issued_at",
            ):
                return False
            changes[token_field] = provider_run_id
            current.update(changes)
            return True

    document = collection.document(execution_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists:
            return False
        current = snapshot.to_dict()
        token_field = (
            "event_token_build_id"
            if current.get("provider") == "cloud_build"
            else "event_token_provider_run_id"
        )
        if not _claim_build_scoped_token(
            current,
            provider_run_id=provider_run_id,
            token_field=token_field,
            issued_field="event_token_issued_at",
        ):
            return False
        changes[token_field] = provider_run_id
        txn.update(document, changes)
        return True

    return write(transaction)


def accept_event(
    execution_id: str, sequence: int, **changes: Any
) -> tuple[dict[str, Any], bool]:
    """Accept only a newer engine event; duplicates remain harmless."""
    changes["event_sequence"] = sequence
    changes["updated_at"] = _now()
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(execution_id)
            if current is None:
                raise KeyError(execution_id)
            if sequence <= int(current.get("event_sequence", 0)):
                return dict(current), False
            _validate_status_change(current, changes)
            current.update(changes)
            return dict(current), True

    document = collection.document(execution_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists:
            raise KeyError(execution_id)
        current = snapshot.to_dict()
        if sequence <= int(current.get("event_sequence", 0)):
            return current, False
        _validate_status_change(current, changes)
        txn.update(document, changes)
        current.update(changes)
        return current, True

    return write(transaction)


def reconcile_submission_absent(execution_id: str) -> dict[str, Any]:
    """Allow one retry only after a reconciler proved no external build exists."""
    current = get(execution_id)
    if current is None:
        raise KeyError(execution_id)
    if current.get("build_id") or current.get("status") not in {
        "submitting",
        "unknown",
    }:
        raise ValueError("Release submission is not uncertain")
    return save(
        execution_id,
        status="submission_pending",
        reconciliation_attempts=int(current.get("reconciliation_attempts", 0)) + 1,
    )


def stage_planner_retry(execution_id: str, *, failed_build_id: str) -> dict[str, Any]:
    """Reserve the single planner-only recovery after verifying terminal identity."""
    if not failed_build_id:
        raise ValueError("Failed Cloud Build identity is required")
    current = get(execution_id)
    if current is None:
        raise KeyError(execution_id)
    if not planner_retry_candidate(current) or current.get("build_id") != failed_build_id:
        raise ValueError("Release execution is not eligible for planner recovery")

    now = _now()
    previous_build_ids = list(current.get("previous_build_ids") or [])
    if failed_build_id not in previous_build_ids:
        previous_build_ids.append(failed_build_id)
    changes = {
        "status": "submission_pending",
        "build_id": "",
        "provider_run_id": "",
        "provider_status": "RETRY_PENDING",
        "logs_url": "",
        "build_name": "",
        "previous_build_ids": previous_build_ids,
        "planner_retry_pending": True,
        "planner_retry_count": 1,
        "planner_retry_checked": True,
        "planner_retry_previous_build_id": failed_build_id,
        "planner_retry_requested_at": now,
        "release_engine_failed": False,
        "release_error": "",
        "error": "",
        "updated_at": now,
    }
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(execution_id)
            if current is None:
                raise KeyError(execution_id)
            if not planner_retry_candidate(current) or current.get("build_id") != failed_build_id:
                raise ValueError("Release execution is not eligible for planner recovery")
            current.update(changes)
            return dict(current)

    document = collection.document(execution_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists:
            raise KeyError(execution_id)
        current = snapshot.to_dict()
        if not planner_retry_candidate(current) or current.get("build_id") != failed_build_id:
            raise ValueError("Release execution is not eligible for planner recovery")
        # This narrowly scoped recovery is the only terminal-state reopening:
        # exact quality evidence is already committed, and the known failed
        # planner step is verified by the reconciler before this reservation.
        txn.update(document, changes)
        current.update(changes)
        return current

    return write(transaction)


def _validate_status_change(current: dict[str, Any], changes: dict[str, Any]) -> None:
    new_status = changes.get("status")
    if not new_status or new_status == current.get("status"):
        return
    old_status = str(current.get("status", "received"))
    if new_status not in _TRANSITIONS.get(old_status, set()):
        raise ValueError(
            f"Invalid release execution transition {old_status}->{new_status}"
        )


def list_for_repository(repository: str, *, limit: int = 50) -> list[dict[str, Any]]:
    repository_names = aliases(repository)
    collection = _collection()
    if collection is None:
        with _lock:
            values = [
                dict(value)
                for value in _memory.values()
                if value.get("repository") in repository_names
            ]
    else:
        # Apply the limit only after deterministic ordering. Firestore's
        # unspecified pre-limit order can otherwise hide the newest execution
        # once a repository has accumulated historical terminal records.
        values = []
        for name in repository_names:
            query = collection.where("repository", "==", name)
            values.extend(snapshot.to_dict() for snapshot in query.stream())
    values.sort(key=lambda value: value.get("created_at", ""), reverse=True)
    return values[:limit]


def find(repository: str, head_sha: str, operation: str) -> dict[str, Any] | None:
    return next(
        (
            value
            for value in list_for_repository(repository, limit=100)
            if value.get("head_sha") == head_sha.lower()
            and value.get("operation") == operation
        ),
        None,
    )


def list_due(*, limit: int = 100) -> list[dict[str, Any]]:
    terminal = {"quality_failed", "no_release", "released", "failed"}
    collection = _collection()
    if collection is None:
        with _lock:
            values = [
                dict(value)
                for value in _memory.values()
                if (
                    value.get("status") not in terminal
                    or planner_retry_candidate(value)
                )
                and not (
                    value.get("operation") == "pr_quality"
                    and value.get("status") == "quality_passed"
                )
            ]
    else:
        # Do not limit before filtering: terminal history is intentionally
        # retained and must never crowd newer active work out of reconciliation.
        values = [
            snapshot.to_dict()
            for snapshot in collection.stream()
            if (
                snapshot.to_dict().get("status") not in terminal
                or planner_retry_candidate(snapshot.to_dict())
            )
            and not (
                snapshot.to_dict().get("operation") == "pr_quality"
                and snapshot.to_dict().get("status") == "quality_passed"
            )
        ]
    values.sort(key=lambda value: value.get("updated_at", ""))
    return values[:limit]


def monthly_cloud_build_minutes(month: str) -> float:
    """Sum completed quality/release build minutes for one UTC YYYY-MM month."""
    collection = _collection()
    if collection is None:
        with _lock:
            values = [dict(value) for value in _memory.values()]
    else:
        values = [snapshot.to_dict() for snapshot in collection.stream()]
    return round(
        sum(
            float(value.get("build_minutes_estimate", 0) or 0)
            for value in values
            if value.get("provider") == "cloud_build"
            and str(value.get("build_finished_at", "")).startswith(month)
        ),
        3,
    )
