"""Authenticate and journal GitHub App webhook deliveries."""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from ..config import config

ALLOWED_EVENTS = frozenset({"pull_request", "push", "workflow_run"})
_memory: dict[str, dict[str, Any]] = {}
_lock = RLock()


def verify_signature(body: bytes, signature: str) -> bool:
    secret = config.release_orchestrator.webhook_secret
    if not secret:
        return bool(config.mock_mode)
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature)


def _collection():
    settings = config.release_orchestrator
    if (
        config.mock_mode
        or not settings.enabled
        or not settings.webhook_delivery_collection
    ):
        return None
    from google.cloud import firestore

    project = config.cloud_build.project_id or config.monitoring.gcp_project_id
    return firestore.Client(project=project or None).collection(
        settings.webhook_delivery_collection
    )


def _id(delivery_id: str) -> str:
    return hashlib.sha256(delivery_id.encode()).hexdigest()


def receive(
    *, delivery_id: str, event: str, repository: str, body: bytes
) -> tuple[dict[str, Any], bool]:
    if not delivery_id or len(delivery_id) > 128:
        raise ValueError("Invalid GitHub delivery ID")
    if event not in ALLOWED_EVENTS:
        raise ValueError("Unsupported GitHub event")
    now = datetime.now(timezone.utc).isoformat()
    delivery = {
        "delivery_id": delivery_id,
        "event": event,
        "repository": repository,
        "payload_sha256": hashlib.sha256(body).hexdigest(),
        "status": "received",
        "received_at": now,
        "updated_at": now,
    }
    key = _id(delivery_id)
    collection = _collection()
    if collection is None:
        with _lock:
            current = _memory.get(key)
            if current:
                if any(
                    current.get(field) != delivery[field]
                    for field in ("payload_sha256", "event", "repository")
                ):
                    raise ValueError("GitHub delivery payload changed")
                return dict(current), False
            _memory[key] = delivery
            return dict(delivery), True

    document = collection.document(key)
    transaction = document._client.transaction()
    from google.cloud.firestore import transactional

    @transactional
    def write(txn):
        snapshot = document.get(transaction=txn)
        if snapshot.exists:
            current = snapshot.to_dict()
            if any(
                current.get(field) != delivery[field]
                for field in ("payload_sha256", "event", "repository")
            ):
                raise ValueError("GitHub delivery payload changed")
            return current, False
        txn.set(document, delivery)
        return delivery, True

    return write(transaction)


def complete(delivery_id: str, **changes: Any) -> None:
    key = _id(delivery_id)
    changes.update(
        {"status": "processed", "updated_at": datetime.now(timezone.utc).isoformat()}
    )
    collection = _collection()
    if collection is None:
        with _lock:
            if key not in _memory:
                raise KeyError(delivery_id)
            _memory[key].update(changes)
        return
    collection.document(key).set(changes, merge=True)
