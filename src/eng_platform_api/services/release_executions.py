"""Durable, idempotent state for PR quality and main release preparation."""

from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Literal
from collections.abc import Iterator

from ..config import config
from .repository_identity import aliases
from .firestore_queries import count_query_at, iter_query_pages
from .quality_profiles import github_actions_only

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
        "superseded",
        "failed",
        "unknown",
    },
    "quality_failed": set(),
    "release_planned": {"publish_pending", "superseded", "failed", "unknown"},
    "publish_pending": {
        "released",
        "release_planned",
        "superseded",
        "failed",
        "unknown",
    },
    "no_release": set(),
    "released": set(),
    "superseded": set(),
    "failed": set(),
    "unknown": {
        "submission_pending",
        "running_quality",
        "release_planned",
        "released",
        "superseded",
        "failed",
    },
}

_memory: dict[str, dict[str, Any]] = {}
_lock = RLock()
_logger = logging.getLogger(__name__)
QUALITY_BOOTSTRAP_TICKET_ID = "artemis-pr172-quality-bootstrap-v1"
_BOOTSTRAP_DOCUMENT_ID = "__bootstrap_ticket_" + QUALITY_BOOTSTRAP_TICKET_ID
_IDENTITY_FIELDS = frozenset(
    {
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
)


def _persistent_bootstrap_collection():
    """The global fence may use memory only in explicit mock mode."""
    collection = _collection()
    if collection is None and not config.mock_mode:
        raise RuntimeError("Persistent quality bootstrap storage is unavailable")
    return collection


def get_quality_bootstrap_ticket() -> dict[str, Any] | None:
    collection = _persistent_bootstrap_collection()
    if collection is None:
        with _lock:
            return deepcopy(_memory.get(_BOOTSTRAP_DOCUMENT_ID))
    snapshot = collection.document(_BOOTSTRAP_DOCUMENT_ID).get()
    return snapshot.to_dict() if snapshot.exists else None


def _mutate(execution_id: str, callback):
    """Read, check and update the same version in a serializable transaction."""
    if execution_id == _BOOTSTRAP_DOCUMENT_ID:
        raise ValueError("Quality bootstrap global ticket is immutable")
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(execution_id)
            if current is None:
                raise KeyError(execution_id)
            if current.get("bootstrap_ticket_id") and not config.mock_mode:
                raise RuntimeError(
                    "Persistent quality bootstrap storage is unavailable"
                )
            changes, result = callback(deepcopy(current))
            if changes:
                current.update(deepcopy(changes))
            return deepcopy(result if result is not None else current)
    document = collection.document(execution_id)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if not snapshot.exists:
            raise KeyError(execution_id)
        current = snapshot.to_dict()
        changes, result = callback(current)
        if changes:
            txn.update(document, changes)
            current.update(changes)
        return result if result is not None else current

    return write(transaction)


def _validate_changes(current: dict[str, Any], changes: dict[str, Any]) -> None:
    if current.get("ticket_id") == QUALITY_BOOTSTRAP_TICKET_ID:
        raise ValueError("Quality bootstrap global ticket is immutable")
    if (current.get("provider_selection") or {}).get("locked") and any(
        key in changes and changes[key] != current.get(key)
        for key in ("provider", "provider_selection", "trigger_event")
    ):
        raise ValueError("Reserved release provider selection is immutable")
    if _IDENTITY_FIELDS.intersection(changes):
        raise ValueError("Release execution identity fields are immutable")
    if any(key.startswith("bootstrap_") for key in changes):
        raise ValueError("Quality bootstrap fields are immutable")
    if current.get("bootstrap_ticket_id"):
        if not config.mock_mode:
            _persistent_bootstrap_collection()
        if any(
            key in changes and changes[key] != current.get(key)
            for key in (
                "provider",
                "build_id",
                "provider_run_id",
                "github_run_id",
                "build_name",
                "logs_url",
                "pull_request_number",
                "repository_id",
                "source_token_issued_at",
                "source_token_build_id",
                "event_token_hash",
                "event_token_issued_at",
                "event_token_build_id",
                "event_token_provider_run_id",
                "previous_build_ids",
            )
        ):
            raise ValueError("Quality bootstrap attempt cannot be reset or rebound")
        if changes.get("status") in {
            "received",
            "waiting_github",
            "submission_pending",
            "submission_failed",
        }:
            raise ValueError("Quality bootstrap attempt cannot be retried")
        if any(
            key.startswith("planner_retry") or key.startswith("reconciliation_attempt")
            for key in changes
        ):
            raise ValueError("Quality bootstrap attempt cannot be retried")
        for field in (
            "event_sequence",
            "pending_report_hash",
            "report_hash",
            "evidence_committed",
        ):
            if current.get(field) and field in changes:
                if field == "event_sequence":
                    if changes[field] < current[field]:
                        raise ValueError("Quality bootstrap evidence cannot be reset")
                elif current[field] != changes[field]:
                    raise ValueError("Quality bootstrap evidence is immutable")
    _validate_status_change(current, changes)


def _require_event_identity(
    current: dict[str, Any],
    *,
    expected_provider: str,
    expected_run_id: str,
    expected_token_hash: str = "",
) -> None:
    if current.get("provider") != expected_provider:
        raise ValueError("Release execution provider changed")
    run_id = str(current.get("provider_run_id", ""))
    if not expected_run_id or run_id != expected_run_id:
        raise ValueError("Release execution run changed")
    if expected_token_hash and current.get("event_token_hash") != expected_token_hash:
        raise ValueError("Release execution event token changed")
    if current.get("bootstrap_ticket_id") and not current.get(
        "bootstrap_build_verified"
    ):
        raise ValueError("Quality bootstrap build has not been verified")


def save_for_provider(
    execution_id: str,
    *,
    expected_provider: str,
    expected_run_id: str | None = None,
    **changes: Any,
) -> dict[str, Any]:
    changes["updated_at"] = _now()

    def change(current):
        if current.get("provider") != expected_provider:
            raise ValueError("Release execution provider changed")
        if (
            expected_run_id is not None
            and str(current.get("provider_run_id", "")) != expected_run_id
        ):
            raise ValueError("Release execution run changed")
        _validate_changes(current, changes)
        return changes, None

    return _mutate(execution_id, change)


def admit_github_run(
    execution_id: str, *, provider_run_id: str, **metadata: Any
) -> dict[str, Any]:
    """Fence authenticated GitHub admission against a simultaneous bootstrap."""
    if not provider_run_id or not provider_run_id.isdigit():
        raise ValueError("GitHub workflow run is invalid")
    changes = {
        **metadata,
        "provider_run_id": provider_run_id,
        "github_run_id": int(provider_run_id),
        "updated_at": _now(),
    }

    def change(current):
        if current.get("provider") != "github_actions" or current.get(
            "bootstrap_ticket_id"
        ):
            raise ValueError("Release execution provider changed")
        if current.get("status") not in {"waiting_github", "running_quality"}:
            raise ValueError("Release execution is not available for GitHub admission")
        if (
            current.get("provider_run_id")
            and str(current["provider_run_id"]) != provider_run_id
        ):
            raise ValueError("Release execution already has a run")
        _validate_changes(current, changes)
        return changes, None

    return _mutate(execution_id, change)


def admit_event_provider(
    execution_id: str,
    *,
    expected_provider: str,
    expected_run_id: str,
    expected_token_hash: str,
) -> dict[str, Any]:
    """Fence the provider before writing immutable pending report evidence."""

    def change(current):
        _require_event_identity(
            current,
            expected_provider=expected_provider,
            expected_run_id=expected_run_id,
            expected_token_hash=expected_token_hash,
        )
        if current.get("status") in {
            "failed",
            "quality_failed",
            "no_release",
            "released",
            "superseded",
        } or (
            current.get("bootstrap_ticket_id")
            and current.get("status") == "quality_passed"
        ):
            raise ValueError("Release execution is terminal")
        return {"event_admitted_at": _now()}, None

    return _mutate(execution_id, change)


def reserve_quality_bootstrap(
    execution_id: str, *, binding: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Consume one deployment-independent global ticket with the fresh execution."""
    binding = deepcopy(binding)
    required = {
        "requested_by",
        "idempotency_key",
        "repository",
        "repository_id",
        "service_name",
        "pull_request_number",
        "head_sha",
        "base_sha",
        "fingerprint",
        "profile_hash",
        "executor_digest",
        "policy_hash",
        "build_request",
        "build_request_hash",
        "nonce",
        "authorization_policy",
        "max_compute_usd",
        "timeout_seconds",
        "dispatch_token_hash",
    }
    if not required.issubset(binding):
        raise ValueError("Quality bootstrap binding is incomplete")
    if (
        binding["repository"] != "diegomad14/cgm-artemis-api"
        or str(binding["repository_id"]) != "1306114845"
        or binding["service_name"] != "cgm-artemis-api"
        or binding["pull_request_number"] != 172
        or binding["base_sha"] != "3c807a789ba78eea95068a3cc0a7a6e537b0bae9"
        or binding["authorization_policy"] != QUALITY_BOOTSTRAP_TICKET_ID
        or str(binding["max_compute_usd"]) != "0.36"
        or binding["timeout_seconds"] != 3600
        or not binding["requested_by"]
        or len(str(binding["idempotency_key"])) != 64
        or not binding["nonce"]
        or len(str(binding["dispatch_token_hash"])) != 64
        or not isinstance(binding["build_request"], dict)
    ):
        raise ValueError("Quality bootstrap does not match the fixed authorization")
    request_hash = hashlib.sha256(
        json.dumps(
            binding["build_request"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    if request_hash != binding["build_request_hash"]:
        raise ValueError("Quality bootstrap build request hash mismatch")
    from .cloud_build_policy import require_new_builds

    require_new_builds()
    collection = _persistent_bootstrap_collection()
    now = _now()

    def validate(current, ticket):
        if ticket is not None:
            if (
                ticket.get("idempotency_key") != binding["idempotency_key"]
                or ticket.get("requested_by") != binding["requested_by"]
                or ticket.get("execution_id") != execution_id
                or any(
                    ticket.get(field) != binding[field]
                    for field in (
                        "repository",
                        "repository_id",
                        "service_name",
                        "pull_request_number",
                        "head_sha",
                        "base_sha",
                        "fingerprint",
                        "profile_hash",
                        "executor_digest",
                        "policy_hash",
                        "authorization_policy",
                    )
                )
            ):
                raise ValueError("Quality bootstrap global ticket is already consumed")
            return None
        if current is None:
            raise KeyError(execution_id)
        if (current.get("provider_selection") or {}).get("locked"):
            raise ValueError(
                "Reserved release provider is immutable; bootstrap cannot change it"
            )
        if (
            current.get("status") != "waiting_github"
            or current.get("provider") != "github_actions"
            or current.get("operation") != "pr_quality"
            or current.get("pull_request_number") != 172
            or any(
                current.get(field)
                for field in (
                    "bootstrap_ticket_id",
                    "provider_run_id",
                    "github_run_id",
                    "build_id",
                    "build_name",
                    "source_token_issued_at",
                    "source_token_build_id",
                    "event_token_hash",
                    "event_token_issued_at",
                    "event_token_build_id",
                    "event_token_provider_run_id",
                    "event_admitted_at",
                    "event_sequence",
                    "engine_event_status",
                    "pending_report_hash",
                    "report_hash",
                    "evidence_committed",
                    "release_plan",
                    "provider_terminal_success",
                    "previous_build_ids",
                    "planner_retry_count",
                )
            )
            or any(
                current.get(field) != binding[field]
                for field in (
                    "repository",
                    "service_name",
                    "head_sha",
                    "base_sha",
                    "fingerprint",
                    "profile_hash",
                    "executor_digest",
                    "policy_hash",
                )
            )
            or execution_id != current.get("execution_id")
        ):
            raise ValueError(
                "Quality bootstrap requires a fresh matching GitHub execution"
            )
        return {
            "provider": "cloud_build",
            "status": "submitting",
            "updated_at": now,
            "bootstrap_ticket_id": QUALITY_BOOTSTRAP_TICKET_ID,
            "bootstrap_attempts": 1,
            "bootstrap_binding": binding,
            "bootstrap_nonce": binding["nonce"],
            "bootstrap_build_request": binding["build_request"],
            "bootstrap_build_request_hash": request_hash,
            "bootstrap_requested_by": binding["requested_by"],
            "bootstrap_idempotency_key": binding["idempotency_key"],
            "bootstrap_reserved_at": now,
        }

    if collection is None:
        with _lock:
            current = _memory.get(execution_id)
            ticket = _memory.get(_BOOTSTRAP_DOCUMENT_ID)
            changes = validate(current, ticket)
            if changes is None:
                assert ticket is not None
                return deepcopy(_memory[ticket["execution_id"]]), False
            assert current is not None
            ticket = {
                **binding,
                "ticket_id": QUALITY_BOOTSTRAP_TICKET_ID,
                "execution_id": execution_id,
                "consumed_at": now,
            }
            _memory[_BOOTSTRAP_DOCUMENT_ID] = deepcopy(ticket)
            current.update(deepcopy(changes))
            return deepcopy(current), True
    document = collection.document(execution_id)
    ticket_document = collection.document(_BOOTSTRAP_DOCUMENT_ID)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        ticket_snapshot = ticket_document.get(transaction=txn)
        ticket = ticket_snapshot.to_dict() if ticket_snapshot.exists else None
        snapshot = document.get(transaction=txn)
        current = snapshot.to_dict() if snapshot.exists else None
        changes = validate(current, ticket)
        if changes is None:
            assert ticket is not None
            saved = collection.document(ticket["execution_id"]).get(transaction=txn)
            if not saved.exists:
                raise RuntimeError("Quality bootstrap execution is unavailable")
            return saved.to_dict(), False
        assert current is not None
        txn.set(
            ticket_document,
            {
                **binding,
                "ticket_id": QUALITY_BOOTSTRAP_TICKET_ID,
                "execution_id": execution_id,
                "consumed_at": now,
            },
        )
        txn.update(document, changes)
        current.update(changes)
        return current, True

    return write(transaction)


def update_quality_bootstrap(
    execution_id: str, *, attempt_nonce: str, changes: dict[str, Any]
) -> dict[str, Any]:
    """Persist the sole attempt's outcome; this API never authorizes a POST."""
    _persistent_bootstrap_collection()
    changes = deepcopy(changes)
    allowed = {
        "status",
        "error",
        "build_id",
        "provider_run_id",
        "provider_status",
        "logs_url",
        "build_name",
        "submission_reconciled_at",
        "bootstrap_build_verified",
        "bootstrap_verified_request_hash",
    }
    if not set(changes).issubset(allowed):
        raise ValueError("Quality bootstrap update contains reserved fields")
    changes["updated_at"] = _now()

    def change(current):
        if (
            current.get("bootstrap_ticket_id") != QUALITY_BOOTSTRAP_TICKET_ID
            or current.get("bootstrap_nonce") != attempt_nonce
        ):
            raise ValueError("Quality bootstrap attempt identity mismatch")
        if "build_id" in changes:
            build_id = str(changes["build_id"])
            if (
                not current.get("bootstrap_dispatch_started")
                or not build_id
                or len(build_id) > 128
                or changes.get("provider_run_id") != build_id
                or changes.get("bootstrap_build_verified") is not True
                or changes.get("bootstrap_verified_request_hash")
                != current.get("bootstrap_build_request_hash")
                or current.get("build_id") not in (None, "", build_id)
                or current.get("provider_run_id") not in (None, "", build_id)
            ):
                raise ValueError("Quality bootstrap build binding is not verified")
        elif any(
            key in changes
            for key in (
                "provider_run_id",
                "bootstrap_build_verified",
                "bootstrap_verified_request_hash",
            )
        ):
            raise ValueError("Quality bootstrap verification requires a build binding")
        if changes.get("status") in {
            "received",
            "waiting_github",
            "submission_pending",
            "submission_failed",
        }:
            raise ValueError("Quality bootstrap attempt cannot be retried")
        _validate_status_change(current, changes)
        return changes, None

    return _mutate(execution_id, change)


def claim_quality_bootstrap_dispatch(
    execution_id: str, *, attempt_nonce: str, dispatch_token: str
) -> bool:
    """Consume the winning process's ephemeral permit before the only POST."""
    _persistent_bootstrap_collection()
    token_hash = hashlib.sha256(dispatch_token.encode()).hexdigest()

    def change(current):
        if (
            current.get("bootstrap_ticket_id") != QUALITY_BOOTSTRAP_TICKET_ID
            or current.get("bootstrap_nonce") != attempt_nonce
            or not dispatch_token
            or current.get("bootstrap_binding", {}).get("dispatch_token_hash")
            != token_hash
            or current.get("bootstrap_dispatch_started")
            or current.get("provider") != "cloud_build"
            or current.get("status") != "submitting"
            or current.get("build_id")
        ):
            return {}, False
        return {
            "bootstrap_dispatch_started": True,
            "bootstrap_dispatch_started_at": _now(),
            "updated_at": _now(),
        }, True

    return _mutate(execution_id, change)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def planner_retry_candidate(
    value: dict[str, Any],
    *,
    allow_remediation: bool = False,
    allow_contract_retry: bool = False,
) -> bool:
    """Whether one safe planner-only recovery is available for this execution."""
    if value.get("bootstrap_ticket_id"):
        return False
    retry_count = int(value.get("planner_retry_count", 0) or 0)
    remediation_image = str(value.get("planner_remediation_retry_image", ""))
    remediation_hash = str(value.get("planner_remediation_retry_hash", ""))
    image_digest = (
        remediation_image.rsplit("@sha256:", 1)[-1]
        if "@sha256:" in remediation_image
        else ""
    )
    remediation_is_pinned = len(image_digest) == 64 and all(
        char in "0123456789abcdef" for char in image_digest
    )
    remediation_hash_is_valid = len(remediation_hash) == 64 and all(
        char in "0123456789abcdef" for char in remediation_hash
    )
    attempt_available = (
        (retry_count == 0 and not value.get("planner_retry_checked"))
        or (
            allow_remediation
            and retry_count == 1
            and value.get("planner_retry_checked")
            and not value.get("planner_remediation_retry_checked")
        )
        or (
            allow_contract_retry
            and retry_count == 2
            and value.get("planner_remediation_retry_checked")
            and not value.get("planner_contract_retry_checked")
            and remediation_is_pinned
            and remediation_hash_is_valid
        )
    )
    return bool(
        value.get("status") == "failed"
        and value.get("provider") == "cloud_build"
        and value.get("operation") == "main_release"
        and value.get("release_engine_failed")
        and value.get("engine_event_status") == "quality_passed"
        and value.get("evidence_committed")
        and value.get("report_hash")
        and attempt_available
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
            if value and value.get("bootstrap_ticket_id") and not config.mock_mode:
                raise RuntimeError(
                    "Persistent quality bootstrap storage is unavailable"
                )
            return deepcopy(value) if value else None
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
    provider_selection: dict | None = None,
    trigger_event: str = "",
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
        "provider_selection": provider_selection,
        "trigger_event": trigger_event,
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
                return deepcopy(current), False
            _memory[execution_id] = value
            return deepcopy(value), True

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
    if _IDENTITY_FIELDS.intersection(changes):
        raise ValueError("Release execution identity fields are immutable")

    def change(current):
        _validate_changes(current, changes)
        return changes, None

    return _mutate(execution_id, change)


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
            if (current.get("provider_selection") or {}).get("locked"):
                raise ValueError("Reserved release provider is immutable")
            if current.get("bootstrap_ticket_id"):
                raise ValueError("Quality bootstrap provider cannot be reset")
            if github_actions_only(str(current.get("service_name") or "")):
                raise ValueError("Service releases run on GitHub Actions only")
            if current.get("provider") == "cloud_build":
                return dict(current), False
            if current.get("provider") != "github_actions":
                raise ValueError("Release execution provider transition is invalid")
            if current.get("status") not in {"received", "waiting_github"}:
                raise ValueError("Release execution is no longer fallback eligible")
            from .cloud_build_policy import require_new_builds

            require_new_builds()
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
        if (current.get("provider_selection") or {}).get("locked"):
            raise ValueError("Reserved release provider is immutable")
        if current.get("bootstrap_ticket_id"):
            raise ValueError("Quality bootstrap provider cannot be reset")
        if github_actions_only(str(current.get("service_name") or "")):
            raise ValueError("Service releases run on GitHub Actions only")
        if current.get("provider") == "cloud_build":
            return current, False
        if current.get("provider") != "github_actions":
            raise ValueError("Release execution provider transition is invalid")
        if current.get("status") not in {"received", "waiting_github"}:
            raise ValueError("Release execution is no longer fallback eligible")
        from .cloud_build_policy import require_new_builds

        require_new_builds()
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
                current.get("bootstrap_ticket_id")
                or current.get("provider") != "cloud_build"
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
            current.get("bootstrap_ticket_id")
            or current.get("provider") != "cloud_build"
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
            if current.get("bootstrap_ticket_id"):
                raise ValueError("Quality bootstrap requires verified build binding")
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
        if current.get("bootstrap_ticket_id"):
            raise ValueError("Quality bootstrap requires verified build binding")
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
    current: dict[str, Any],
    *,
    provider_run_id: str,
    token_field: str,
    issued_field: str,
) -> bool:
    provider = current.get("provider")
    if current.get("bootstrap_ticket_id"):
        if not config.mock_mode:
            _persistent_bootstrap_collection()
        if not current.get("bootstrap_build_verified"):
            return False
        if current.get("status") in {
            "failed",
            "quality_failed",
            "no_release",
            "released",
            "superseded",
            "quality_passed",
        }:
            return False
    if not provider_run_id:
        return False
    if provider == "cloud_build":
        if current.get("build_id") != provider_run_id:
            return False
        previous = set(current.get("previous_build_ids") or [])
    elif provider == "github_actions" and token_field in {
        "event_token_provider_run_id",
        "source_token_provider_run_id",
    }:
        if str(current.get("provider_run_id", "")) != provider_run_id:
            return False
        if token_field == "source_token_provider_run_id" and current.get(
            "status"
        ) not in {"waiting_github", "running_quality"}:
            return False
        previous = set()
    else:
        return False
    issued_for = str(current.get(token_field, ""))
    if issued_for == provider_run_id:
        return False
    if issued_for and issued_for not in previous:
        return False
    if (
        not issued_for
        and current.get(issued_field)
        and (not previous or provider_run_id in previous)
    ):
        # Backward compatibility for executions that recorded only the
        # one-time timestamp before token claims were scoped to build IDs.
        return False
    return True


def claim_source_token(execution_id: str, *, provider_run_id: str) -> bool:
    """Allow one repository-scoped read token per verified provider attempt."""
    now = _now()
    changes = {
        "source_token_issued_at": now,
        "updated_at": now,
    }
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(execution_id)
            token_field = (
                "source_token_build_id"
                if current and current.get("provider") == "cloud_build"
                else "source_token_provider_run_id"
            )
            if current is None or not _claim_build_scoped_token(
                current,
                provider_run_id=provider_run_id,
                token_field=token_field,
                issued_field="source_token_issued_at",
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
            "source_token_build_id"
            if current.get("provider") == "cloud_build"
            else "source_token_provider_run_id"
        )
        if not _claim_build_scoped_token(
            current,
            provider_run_id=provider_run_id,
            token_field=token_field,
            issued_field="source_token_issued_at",
        ):
            return False
        changes[token_field] = provider_run_id
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
    execution_id: str,
    sequence: int,
    *,
    expected_provider: str = "",
    expected_run_id: str = "",
    expected_token_hash: str = "",
    **changes: Any,
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
            if expected_provider:
                _require_event_identity(
                    current,
                    expected_provider=expected_provider,
                    expected_run_id=expected_run_id,
                    expected_token_hash=expected_token_hash,
                )
            elif current.get("bootstrap_ticket_id"):
                raise ValueError(
                    "Quality bootstrap event requires verified provider identity"
                )
            if sequence <= int(current.get("event_sequence", 0)):
                return dict(current), False
            _validate_changes(current, changes)
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
        if expected_provider:
            _require_event_identity(
                current,
                expected_provider=expected_provider,
                expected_run_id=expected_run_id,
                expected_token_hash=expected_token_hash,
            )
        elif current.get("bootstrap_ticket_id"):
            raise ValueError(
                "Quality bootstrap event requires verified provider identity"
            )
        if sequence <= int(current.get("event_sequence", 0)):
            return current, False
        _validate_changes(current, changes)
        txn.update(document, changes)
        current.update(changes)
        return current, True

    return write(transaction)


def reconcile_submission_absent(execution_id: str) -> dict[str, Any]:
    """Allow one retry only after a reconciler proved no external build exists."""
    current = get(execution_id)
    if current is None:
        raise KeyError(execution_id)
    if current.get("bootstrap_ticket_id"):
        raise ValueError("Quality bootstrap attempt cannot be retried")
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


def stage_planner_retry(
    execution_id: str,
    *,
    failed_build_id: str,
    planner_image: str = "",
    planner_hash_value: str = "",
    remediation: bool = False,
    contract_retry: bool = False,
    previous_planner_image: str = "",
) -> dict[str, Any]:
    """Reserve a narrowly gated planner-only recovery after identity checks."""
    from .cloud_build_policy import require_new_builds

    require_new_builds()
    if not failed_build_id:
        raise ValueError("Failed Cloud Build identity is required")
    current = get(execution_id)
    if current is None:
        raise KeyError(execution_id)
    if (
        not planner_retry_candidate(
            current,
            allow_remediation=remediation,
            allow_contract_retry=contract_retry,
        )
        or current.get("build_id") != failed_build_id
    ):
        raise ValueError("Release execution is not eligible for planner recovery")

    retry_count = int(current.get("planner_retry_count", 0) or 0)
    if not planner_image or "@sha256:" not in planner_image:
        raise ValueError("A digest-pinned planner image is required")
    stored_previous_image = str(current.get("planner_retry_image", ""))
    previous_planner_image = stored_previous_image or previous_planner_image
    if remediation and (
        retry_count != 1
        or not previous_planner_image
        or previous_planner_image == planner_image
    ):
        raise ValueError("Planner remediation requires a different pinned image")
    if contract_retry and (
        retry_count != 2
        or not current.get("planner_remediation_retry_checked")
        or current.get("planner_contract_retry_checked")
        or not planner_hash_value
        or not current.get("planner_remediation_retry_image")
        or current.get("planner_remediation_retry_image") != planner_image
        or current.get("planner_remediation_retry_hash") != planner_hash_value
    ):
        raise ValueError("Planner contract retry is not authorized")
    if not remediation and retry_count != 0:
        if not contract_retry:
            raise ValueError("Planner retry budget is already consumed")

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
        "planner_retry_count": retry_count + 1,
        "planner_retry_checked": True,
        "planner_retry_previous_build_id": failed_build_id,
        "planner_retry_requested_at": now,
        "release_engine_failed": False,
        "release_error": "",
        "error": "",
        "updated_at": now,
    }
    if remediation:
        changes.update(
            {
                "planner_remediation_retry_checked": True,
                "planner_remediation_retry_pending": True,
                "planner_remediation_retry_previous_build_id": failed_build_id,
                "planner_remediation_retry_image": planner_image,
                "planner_remediation_retry_hash": planner_hash_value,
                "planner_retry_image": previous_planner_image,
            }
        )
    elif contract_retry:
        changes.update(
            {
                "planner_contract_retry_checked": True,
                "planner_contract_retry_pending": True,
                "planner_contract_retry_previous_build_id": failed_build_id,
                "planner_contract_retry_image": planner_image,
                "planner_contract_retry_hash": planner_hash_value,
            }
        )
    else:
        changes["planner_retry_image"] = planner_image
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(execution_id)
            if current is None:
                raise KeyError(execution_id)
            if (
                not planner_retry_candidate(
                    current,
                    allow_remediation=remediation,
                    allow_contract_retry=contract_retry,
                )
                or current.get("build_id") != failed_build_id
            ):
                raise ValueError(
                    "Release execution is not eligible for planner recovery"
                )
            retry_count = int(current.get("planner_retry_count", 0) or 0)
            previous_for_write = (
                str(current.get("planner_retry_image", "")) or previous_planner_image
            )
            if (
                not planner_image
                or "@sha256:" not in planner_image
                or (
                    remediation
                    and (
                        retry_count != 1
                        or previous_for_write == planner_image
                        or not planner_hash_value
                    )
                )
                or (
                    contract_retry
                    and (
                        retry_count != 2
                        or not planner_hash_value
                        or current.get("planner_contract_retry_checked")
                        or current.get("planner_remediation_retry_image")
                        != planner_image
                        or current.get("planner_remediation_retry_hash")
                        != planner_hash_value
                    )
                )
                or (not remediation and retry_count != 0)
                and not contract_retry
            ):
                raise ValueError("Planner remediation image is not authorized")
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
        if (
            not planner_retry_candidate(
                current,
                allow_remediation=remediation,
                allow_contract_retry=contract_retry,
            )
            or current.get("build_id") != failed_build_id
        ):
            raise ValueError("Release execution is not eligible for planner recovery")
        retry_count = int(current.get("planner_retry_count", 0) or 0)
        previous_for_write = (
            str(current.get("planner_retry_image", "")) or previous_planner_image
        )
        if (
            not planner_image
            or "@sha256:" not in planner_image
            or (
                remediation
                and (
                    retry_count != 1
                    or previous_for_write == planner_image
                    or not planner_hash_value
                )
            )
            or (
                contract_retry
                and (
                    retry_count != 2
                    or not planner_hash_value
                    or current.get("planner_contract_retry_checked")
                    or current.get("planner_remediation_retry_image") != planner_image
                    or current.get("planner_remediation_retry_hash")
                    != planner_hash_value
                )
            )
            or (not remediation and retry_count != 0)
            and not contract_retry
        ):
            raise ValueError("Planner remediation image is not authorized")
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
    if current.get("bootstrap_ticket_id") and old_status == "quality_passed":
        raise ValueError("Completed quality bootstrap cannot be reopened")
    if new_status not in _TRANSITIONS.get(old_status, set()):
        raise ValueError(
            f"Invalid release execution transition {old_status}->{new_status}"
        )


def _excluded_fence_missing_field(
    collection, field, read_time, repository=None, *, allowed_values=None
) -> int:
    """The immutable bootstrap fence is metadata, not a legacy execution."""
    snapshot = collection.document(_BOOTSTRAP_DOCUMENT_ID).get(read_time=read_time)
    if not snapshot.exists:
        return 0
    fence = snapshot.to_dict()
    return int(
        fence.get("ticket_id") == QUALITY_BOOTSTRAP_TICKET_ID
        and (repository is None or fence.get("repository") == repository)
        and (
            fence.get(field) not in allowed_values
            if allowed_values is not None
            else fence.get(field) is None
            if field == "status"
            else field not in fence
        )
    )


def list_for_repository(repository: str, *, limit: int = 50) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    repository_names = aliases(repository)
    collection = _collection()
    if collection is None:
        with _lock:
            values = [
                dict(value)
                for value in _memory.values()
                if value.get("repository") in repository_names
                and value.get("ticket_id") != QUALITY_BOOTSTRAP_TICKET_ID
            ]
    else:
        # The newest `limit` valid rows from each alias suffice for the global
        # newest `limit`. Excluded bootstrap fences must not consume that cap.
        values = []
        read_time = None
        for name in repository_names:
            base_query = collection.where("repository", "==", name)
            total, read_time = count_query_at(base_query, read_time=read_time)
            ordered_count, _ = count_query_at(
                base_query.order_by("created_at", direction="DESCENDING"),
                read_time=read_time,
            )
            query = base_query.order_by("created_at", direction="DESCENDING").order_by(
                "__name__", direction="DESCENDING"
            )
            missing = total - ordered_count
            if missing:
                missing -= _excluded_fence_missing_field(
                    collection, "created_at", read_time, name
                )
            legacy = missing != 0
            if legacy:
                _logger.warning(
                    "Release history uses legacy timestamp compatibility scan"
                )
                query = base_query
            kept = 0
            for snapshot in iter_query_pages(
                query, page_size=max(1, limit), read_time=read_time
            ):
                value = snapshot.to_dict()
                if value.get("ticket_id") == QUALITY_BOOTSTRAP_TICKET_ID:
                    continue
                values.append(value)
                kept += 1
                if kept >= limit and not legacy:
                    break
    values.sort(
        key=lambda value: (value.get("created_at", ""), value.get("execution_id", "")),
        reverse=True,
    )
    return values[:limit]


def find(repository: str, head_sha: str, operation: str) -> dict[str, Any] | None:
    collection = _collection()
    if collection is None:
        with _lock:
            values = [
                dict(value)
                for value in _memory.values()
                if value.get("repository") in aliases(repository)
                and value.get("head_sha") == head_sha.lower()
                and value.get("operation") == operation
                and value.get("ticket_id") != QUALITY_BOOTSTRAP_TICKET_ID
            ]
    else:
        values = []
        read_time = None
        for name in aliases(repository):
            query = (
                collection.where("repository", "==", name)
                .where("head_sha", "==", head_sha.lower())
                .where("operation", "==", operation)
            )
            _, read_time = count_query_at(query, read_time=read_time)
            values.extend(
                snapshot.to_dict()
                for snapshot in iter_query_pages(query, read_time=read_time)
            )
        values = [
            value
            for value in values
            if value.get("ticket_id") != QUALITY_BOOTSTRAP_TICKET_ID
        ]
    return max(
        values,
        key=lambda value: (value.get("created_at", ""), value.get("execution_id", "")),
        default=None,
    )


def iter_due(*, page_size: int = 100) -> Iterator[dict[str, Any]]:
    """All reconciliation candidates, globally oldest first, without a work cap.

    Missing/null legacy statuses are detected at the same server snapshot and
    retain the original scan/filter semantics. Index errors never trigger a
    scan fallback. The separate bootstrap fence is never reconciled.
    """
    terminal = {"quality_failed", "no_release", "released", "superseded", "failed"}
    collection = _collection()
    if collection is None:
        with _lock:
            values = [
                dict(value)
                for value in _memory.values()
                if (
                    value.get("status") not in terminal
                    or planner_retry_candidate(
                        value,
                        allow_remediation=True,
                        allow_contract_retry=True,
                    )
                )
                and value.get("ticket_id") != QUALITY_BOOTSTRAP_TICKET_ID
                and not (
                    value.get("operation") == "pr_quality"
                    and value.get("status") == "quality_passed"
                )
            ]
    else:
        total, read_time = count_query_at(collection)
        indexed_statuses, _ = count_query_at(
            collection.where("status", "!=", None), read_time=read_time
        )
        known_operations = ["main_release", "pr_quality"]
        indexed_operations, _ = count_query_at(
            collection.where("operation", "in", known_operations), read_time=read_time
        )
        legacy_operations = total - indexed_operations
        if legacy_operations:
            legacy_operations -= _excluded_fence_missing_field(
                collection, "operation", read_time, allowed_values=known_operations
            )
        passed_query = collection.where("status", "==", "quality_passed")
        if legacy_operations:
            _logger.warning(
                "Release reconciliation uses legacy operation compatibility query"
            )
        else:
            passed_query = passed_query.where("operation", "==", "main_release")
        queries = [
            # Passed PR quality is retained history, not active work. Main
            # quality_passed still needs its planner, hence the second query.
            collection.where("status", "not-in", sorted(terminal | {"quality_passed"})),
            passed_query,
            # Failed planner-only recovery is the deliberate terminal exception.
            collection.where("status", "==", "failed")
            .where("provider", "==", "cloud_build")
            .where("operation", "==", "main_release")
            .where("release_engine_failed", "==", True)
            .where("engine_event_status", "==", "quality_passed")
            .where("evidence_committed", "==", True),
        ]
        missing = total - indexed_statuses
        if missing:
            missing -= _excluded_fence_missing_field(collection, "status", read_time)
        if missing:
            _logger.warning(
                "Release reconciliation uses legacy status compatibility scan"
            )
            queries = [collection]
        by_id = {}
        for query in queries:
            for snapshot in iter_query_pages(
                query, page_size=page_size, read_time=read_time
            ):
                value = snapshot.to_dict()
                if (
                    value.get("ticket_id") != QUALITY_BOOTSTRAP_TICKET_ID
                    and (
                        value.get("status") not in terminal
                        or planner_retry_candidate(
                            value, allow_remediation=True, allow_contract_retry=True
                        )
                    )
                    and not (
                        value.get("operation") == "pr_quality"
                        and value.get("status") == "quality_passed"
                    )
                ):
                    by_id[value["execution_id"]] = value
        values = list(by_id.values())
    values.sort(
        key=lambda value: (value.get("updated_at", ""), value.get("execution_id", ""))
    )
    yield from values


def list_due(*, limit: int = 100) -> list[dict[str, Any]]:
    """Compatibility reader; the scheduler uses exhaustive ``iter_due``."""
    return list(iter_due())[:limit]


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


def monthly_cloud_build_usage(month: str) -> dict[str, Any]:
    """Cloud Build minutes for one UTC month across both execution planes."""
    release_minutes = monthly_cloud_build_minutes(month)
    from . import deployment_executions

    deployment_minutes = deployment_executions.monthly_cloud_build_minutes(month)
    price = config.release_orchestrator.build_minute_price_usd
    total = round(release_minutes + deployment_minutes, 3)
    return {
        "month": month,
        "release_minutes": release_minutes,
        "deployment_minutes": deployment_minutes,
        "total_minutes": total,
        "estimated_cost_usd": round(total * price, 4),
        "minute_price_usd": price,
        "alert_thresholds": list(config.release_orchestrator.usage_alert_minutes),
        "alert_thresholds_reached": [
            threshold
            for threshold in config.release_orchestrator.usage_alert_minutes
            if total >= threshold
        ],
    }
