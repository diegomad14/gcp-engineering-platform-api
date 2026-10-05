"""Dedicated Firestore control plane. No process-local production fallback."""

from __future__ import annotations

from contextlib import contextmanager
import re
import time
from typing import Any, Callable

from ..config import config
from .database_registry import DatabaseUnavailable

Document = dict[str, Any]
Transform = Callable[[dict[str, Document | None]], dict[str, Document | None]]
_test_backend: Any = None
_client: Any = None
_identity: tuple[str, str] | None = None
KINDS = frozenset({"session", "workspace", "execution", "export", "control", "request"})


class _TransactionAPI:
    """Bound the three RPCs for which the SDK decorator omits call options."""

    def __init__(self, api):
        self.api = api

    def begin_transaction(self, **kwargs):
        return self.api.begin_transaction(**{**kwargs, "timeout": 4, "retry": None})

    def commit(self, **kwargs):
        return self.api.commit(**{**kwargs, "timeout": 4, "retry": None})

    def rollback(self, **kwargs):
        return self.api.rollback(**{**kwargs, "timeout": 4, "retry": None})


class _TransactionClient:
    def __init__(self, client):
        self.client = client
        self._firestore_api = _TransactionAPI(client._firestore_api)

    def __getattr__(self, name):
        return getattr(self.client, name)


def key(kind: str, identity: str) -> str:
    if kind not in KINDS or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", identity):
        raise DatabaseUnavailable("Invalid database control identity")
    return f"{kind}:{identity}"


@contextmanager
def testing_backend(backend):
    """Explicit dependency injection for tests; never selected from runtime flags."""
    global _test_backend
    previous = _test_backend
    _test_backend = backend
    try:
        yield backend
    finally:
        _test_backend = previous


def _collection():
    global _client, _identity
    settings = config.databases
    identity = (settings.project_id, settings.collection)
    if not identity[0] or not re.fullmatch(
        r"eng_platform_database_[a-z0-9_]{1,48}", identity[1]
    ):
        raise DatabaseUnavailable("Database control store is unavailable")
    if _identity != identity or _client is None:
        from google.cloud import firestore

        _client = firestore.Client(project=identity[0])
        _identity = identity
    return _client.collection(identity[1])


def get(kind: str, identity: str) -> Document | None:
    label = key(kind, identity)
    try:
        if _test_backend is not None:
            return _test_backend.get(kind, identity)
        snapshot = (
            _collection()
            .document(label.replace(":", "_", 1))
            .get(timeout=4, retry=None)
        )
        return snapshot.to_dict() if snapshot.exists else None
    except DatabaseUnavailable:
        raise
    except Exception:
        raise DatabaseUnavailable("Database control store is unavailable") from None


def mutate(references: list[tuple[str, str]], transform: Transform) -> None:
    labels = [key(*reference) for reference in references]
    if len(set(labels)) != len(labels) or len(labels) > 32:
        raise DatabaseUnavailable("Invalid database control transaction")
    try:
        if _test_backend is not None:
            _test_backend.mutate(references, transform)
            return
        from google.cloud.firestore import transactional

        collection = _collection()
        documents = {
            label: collection.document(label.replace(":", "_", 1)) for label in labels
        }
        transaction = _client.transaction(max_attempts=3)
        # The facade belongs to this transaction only. Shared client/transport
        # methods remain untouched; SDK CAS conflict retries stay intact.
        transaction._client = _TransactionClient(_client)
        deadline = time.monotonic() + 10

        def check_deadline():
            if time.monotonic() >= deadline:
                raise DatabaseUnavailable("Database control transaction timed out")

        @transactional
        def write(txn):
            check_deadline()
            values = {}
            for label, document in documents.items():
                check_deadline()
                snapshot = document.get(transaction=txn, timeout=4, retry=None)
                values[label] = snapshot.to_dict() if snapshot.exists else None
            check_deadline()
            changes = transform(values)
            if not set(changes).issubset(documents):
                raise DatabaseUnavailable("Invalid database control transaction")
            for label, value in changes.items():
                if value is None:
                    txn.delete(documents[label])
                else:
                    txn.set(documents[label], value)

        write(transaction)
    except (DatabaseUnavailable,):
        raise
    except Exception as exc:
        # Preserve application decisions (HTTP denial/cancel), never SDK details.
        from fastapi import HTTPException

        if isinstance(exc, HTTPException):
            raise
        raise DatabaseUnavailable("Database control store is unavailable") from None


def find(kind: str, field: str, value: Any, *, limit: int = 100) -> list[Document]:
    key(kind, "scan")
    if (
        field
        not in {"session_id", "workspace_id", "execution_id", "expires_at", "state"}
        or not 1 <= limit <= 256
    ):
        raise DatabaseUnavailable("Invalid database control lookup")
    try:
        if _test_backend is not None:
            return _test_backend.find(kind, field, value, limit=limit)
        from google.cloud.firestore_v1.base_query import FieldFilter

        operator = "<=" if field == "expires_at" else "=="
        query = (
            _collection()
            .where(filter=FieldFilter("kind", "==", kind))
            .where(filter=FieldFilter(field, operator, value))
            .limit(limit)
        )
        return [snapshot.to_dict() for snapshot in query.stream(timeout=4, retry=None)]
    except Exception:
        raise DatabaseUnavailable("Database control store is unavailable") from None
