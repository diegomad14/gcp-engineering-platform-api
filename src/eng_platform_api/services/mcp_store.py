"""Private persistence for MCP OAuth state and sanitized audit records."""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from ..config import config

_memory: dict[str, dict[str, dict[str, Any]]] = {
    "client": {},
    "state": {},
    "code": {},
    "access": {},
    "refresh": {},
    "audit": {},
}


def opaque_id() -> str:
    """Create opaque values without storing their plaintext credentials."""
    return secrets.token_urlsafe(32)


def token_key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def now() -> datetime:
    return datetime.now(timezone.utc)


def expires_in(seconds: int) -> str:
    return (now() + timedelta(seconds=seconds)).isoformat()


def _collection(kind: str):
    if config.mock_mode or not config.mcp.oauth_collection:
        return None
    from google.cloud import firestore

    project = config.monitoring.gcp_project_id or config.cloud_build.project_id
    client = firestore.Client(project=project or None)
    return (
        client.collection(config.mcp.oauth_collection)
        .document(kind)
        .collection("records")
    )


def save(kind: str, key: str, value: dict[str, Any]) -> None:
    record = {**value, "updated_at": now().isoformat()}
    collection = _collection(kind)
    if collection is None:
        _memory.setdefault(kind, {})[key] = record
        return
    collection.document(key).set(record)


def get(kind: str, key: str) -> dict[str, Any] | None:
    collection = _collection(kind)
    if collection is None:
        value = _memory.setdefault(kind, {}).get(key)
        return dict(value) if value else None
    snapshot = collection.document(key).get()
    return snapshot.to_dict() if snapshot.exists else None


def delete(kind: str, key: str) -> None:
    collection = _collection(kind)
    if collection is None:
        _memory.setdefault(kind, {}).pop(key, None)
        return
    collection.document(key).delete()


def delete_access_session(session_id: str) -> None:
    """Revoke the old short-lived access token during refresh-token rotation."""
    if config.mock_mode or not config.mcp.oauth_collection:
        for key, value in list(_memory["access"].items()):
            if value.get("session_id") == session_id:
                _memory["access"].pop(key, None)
        return
    collection = _collection("access")
    if collection is None:
        return
    for snapshot in collection.where("session_id", "==", session_id).stream():
        snapshot.reference.delete()


def save_audit(value: dict[str, Any]) -> None:
    record = {**value, "created_at": now().isoformat()}
    if config.mock_mode or not config.mcp.audit_collection:
        _memory["audit"][opaque_id()] = record
        return
    from google.cloud import firestore

    project = config.monitoring.gcp_project_id or config.cloud_build.project_id
    firestore.Client(project=project or None).collection(
        config.mcp.audit_collection
    ).add(record)


def mutation_count(subject: str) -> int:
    cutoff = now() - timedelta(hours=1)
    if config.mock_mode or not config.mcp.audit_collection:
        return sum(
            1
            for record in _memory["audit"].values()
            if record.get("subject") == subject
            and record.get("mutation") is True
            and datetime.fromisoformat(record["created_at"]) >= cutoff
        )
    from google.cloud import firestore

    project = config.monitoring.gcp_project_id or config.cloud_build.project_id
    query = (
        firestore.Client(project=project or None)
        .collection(config.mcp.audit_collection)
        .where("subject", "==", subject)
        .where("mutation", "==", True)
        .where("created_at", ">=", cutoff.isoformat())
    )
    return sum(1 for _ in query.stream())
