"""Atomic one-time consumption. Production always requires Firestore."""

from __future__ import annotations

import os
import threading
from typing import Any

from ..config import config
from . import deployment_store

_lock = threading.RLock()
_mock_entries: dict[str, dict[str, Any]] = {}


def _firestore_collection():
    collection_name = deployment_store.release_authorization_collection()
    if not collection_name:
        return None
    project_id = os.getenv("ENG_PLATFORM_GCP_PROJECT_ID", "").strip()
    if not project_id:
        raise RuntimeError("Release authorization project is not configured")
    return deployment_store.firestore_client(project_id).collection(collection_name)


def consume(jti: str, record: dict[str, Any], *, require_durable: bool = False) -> bool:
    collection = _firestore_collection()
    if collection is not None:
        try:
            collection.document(jti).create(record)
            return True
        except Exception as exc:
            if exc.__class__.__name__ in {"AlreadyExists", "Conflict"}:
                return False
            raise
    if require_durable or not config.mock_mode:
        raise RuntimeError("Durable release authorization store is not configured")
    # In-memory simulation only. No request data is written to local files.
    with _lock:
        if jti in _mock_entries:
            return False
        _mock_entries[jti] = record
        return True


def get(jti: str) -> dict[str, Any] | None:
    """Return a consumed authorization without exposing its token."""
    if not jti:
        return None
    collection = _firestore_collection()
    if collection is not None:
        snapshot = collection.document(jti).get()
        if not getattr(snapshot, "exists", False):
            return None
        record = snapshot.to_dict() or {}
        return record if isinstance(record, dict) else None
    if not config.mock_mode:
        raise RuntimeError("Durable release authorization store is not configured")
    with _lock:
        record = _mock_entries.get(jti)
        return dict(record) if record else None


def bind(
    jti: str,
    binding: dict[str, Any],
    *,
    require_durable: bool = False,
) -> bool:
    """Atomically bind a consumed ticket to one execution scope and effects.

    One lifecycle authorization covers the complete operation, so it may
    safely accumulate several exact effect digests.  The scope and immutable
    execution context remain single-valued; a token cannot be replayed for a
    different release, target, owner context, or scope.
    """

    def compatible(
        current: Any, incoming: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        if not current:
            normalized = dict(incoming)
            digest = normalized.get("effect_digest", "")
            normalized["effect_digests"] = [digest] if digest else []
            return True, normalized
        if not isinstance(current, dict) or not isinstance(incoming, dict):
            return False, {}
        if any(
            current.get(key) != incoming.get(key)
            for key in ("scope", "scope_key", "context")
        ):
            return False, {}
        normalized = dict(current)
        digests = list(normalized.get("effect_digests") or [])
        legacy_digest = normalized.get("effect_digest", "")
        if legacy_digest and legacy_digest not in digests:
            digests.insert(0, legacy_digest)
        incoming_digest = incoming.get("effect_digest", "")
        if incoming_digest and incoming_digest not in digests:
            digests.append(incoming_digest)
        normalized["effect_digests"] = digests
        if not normalized.get("effect_digest") and incoming_digest:
            normalized["effect_digest"] = incoming_digest
        return True, normalized

    collection = _firestore_collection()
    if collection is not None:
        from google.cloud import firestore

        document = collection.document(jti)
        client = getattr(
            collection, "_client", None
        ) or deployment_store.firestore_client("")
        transaction = client.transaction(max_attempts=20)

        @firestore.transactional
        def bind_transaction(transaction):
            snapshot = document.get(transaction=transaction)
            if not getattr(snapshot, "exists", False):
                return False
            record = snapshot.to_dict() or {}
            current = record.get("execution_binding")
            can_bind, normalized = compatible(current, binding)
            if not can_bind:
                return False
            if current != normalized:
                transaction.update(document, {"execution_binding": normalized})
            return True

        return bool(bind_transaction(transaction))
    if require_durable or not config.mock_mode:
        raise RuntimeError("Durable release authorization store is not configured")
    with _lock:
        record = _mock_entries.get(jti)
        if not record:
            return False
        current = record.get("execution_binding")
        can_bind, normalized = compatible(current, binding)
        if not can_bind:
            return False
        if current != normalized:
            record["execution_binding"] = normalized
        return True
