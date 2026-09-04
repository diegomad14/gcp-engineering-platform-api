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
