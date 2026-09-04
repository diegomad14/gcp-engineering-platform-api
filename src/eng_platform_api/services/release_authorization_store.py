"""Atomic one-time consumption for release authorizations."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from . import deployment_store

_lock = threading.RLock()
_DEFAULT_STORE_PATH = Path(
    os.getenv(
        "ENG_PLATFORM_RELEASE_AUTH_STORE_PATH", "data/release_authorizations.json"
    )
)


def _path() -> Path:
    return (
        _DEFAULT_STORE_PATH
        if _DEFAULT_STORE_PATH.is_absolute()
        else Path.cwd() / _DEFAULT_STORE_PATH
    )


def _firestore_collection():
    collection_name = deployment_store.release_authorization_collection()
    if not collection_name:
        return None
    project_id = os.getenv("ENG_PLATFORM_GCP_PROJECT_ID", "").strip()
    return deployment_store.firestore_client(project_id).collection(collection_name)


def consume(jti: str, record: dict[str, Any], *, require_durable: bool = False) -> bool:
    if require_durable and (
        not deployment_store.release_authorization_collection()
        or not os.getenv("ENG_PLATFORM_GCP_PROJECT_ID", "").strip()
    ):
        raise RuntimeError("Durable release authorization store is not configured")
    collection = _firestore_collection()
    if collection is not None:
        try:
            collection.document(jti).create(record)
            return True
        except Exception as exc:
            if exc.__class__.__name__ in {"AlreadyExists", "Conflict"}:
                return False
            raise
    with _lock:
        path = _path()
        try:
            entries = (
                json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            )
        except (OSError, json.JSONDecodeError):
            entries = {}
        if not isinstance(entries, dict):
            entries = {}
        if jti in entries:
            return False
        entries[jti] = record
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        return True
