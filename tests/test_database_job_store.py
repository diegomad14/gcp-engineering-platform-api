"""Dedicated Firestore namespace, bounded native calls and atomic transforms."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

from fastapi import HTTPException
from google.api_core.exceptions import Aborted, DeadlineExceeded
from google.cloud import firestore
from google.cloud.firestore_v1.transaction import Transaction, transactional
import pytest

from eng_platform_api.config import config
from eng_platform_api.services import database_job_store as store
from eng_platform_api.services.database_registry import DatabaseUnavailable


class MemoryControlBackend:
    """Explicit test adapter; each mutation reads a snapshot before replacement."""

    def __init__(self):
        self.values = {}

    def get(self, kind, identity):
        return deepcopy(self.values.get(store.key(kind, identity)))

    def mutate(self, references, transform):
        values = {store.key(*ref): self.get(*ref) for ref in references}
        changes = transform(values)
        if not set(changes).issubset(values):
            raise ValueError("Undeclared transaction write")
        for key, value in changes.items():
            if value is None:
                self.values.pop(key, None)
            else:
                self.values[key] = deepcopy(value)

    def find(self, kind, field, value, *, limit):
        return [
            deepcopy(record)
            for key, record in self.values.items()
            if key.startswith(kind + ":") and record.get(field) == value
        ][:limit]


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setattr(store, "_test_backend", None)
    monkeypatch.setattr(store, "_client", None)
    monkeypatch.setattr(store, "_identity", None)
    monkeypatch.setattr(config.databases, "project_id", "database-test-project")
    monkeypatch.setattr(config.databases, "collection", "eng_platform_database_control")
    collection = Mock()
    documents = {}

    def document(key):
        if key not in documents:
            documents[key] = Mock()
            documents[key].get.return_value = SimpleNamespace(
                exists=False, to_dict=lambda: None
            )
        return documents[key]

    collection.document.side_effect = document
    collection.where.return_value = collection
    collection.limit.return_value = collection
    client = Mock()
    client.collection.return_value = collection
    constructor = Mock(return_value=client)
    monkeypatch.setattr(firestore, "Client", constructor)
    monkeypatch.setattr(firestore, "transactional", lambda function: function)
    return client, collection, documents, constructor


def test_native_get_uses_dedicated_project_namespaced_document_and_bounded_rpc(native):
    client, collection, documents, constructor = native
    assert store.get("session", "opaque") is None
    constructor.assert_called_once_with(project="database-test-project")
    client.collection.assert_called_once_with("eng_platform_database_control")
    collection.document.assert_called_once_with("session_opaque")
    documents["session_opaque"].get.assert_called_once_with(timeout=4, retry=None)
    documents["session_opaque"].get.return_value = SimpleNamespace(
        exists=True, to_dict=lambda: {"kind": "session", "revoked_at": 0}
    )
    assert store.get("session", "opaque")["revoked_at"] == 0


def test_project_or_collection_change_recreates_client(native, monkeypatch):
    _, _, _, constructor = native
    store.get("control", "active_policy")
    store.get("control", "budget")
    assert constructor.call_count == 1
    monkeypatch.setattr(config.databases, "project_id", "other-database-project")
    store.get("control", "active_policy")
    assert constructor.call_count == 2
    monkeypatch.setattr(config.databases, "collection", "eng_platform_database_other")
    store.get("control", "active_policy")
    assert constructor.call_count == 3


@pytest.mark.parametrize(
    "field,value",
    [
        ("project_id", ""),
        ("collection", ""),
        ("collection", "deployment_executions"),
        ("collection", "eng_platform_database_../../"),
    ],
)
@pytest.mark.parametrize("operation", ["get", "mutate", "find"])
def test_missing_or_shared_production_namespace_has_no_fallback(
    native, monkeypatch, field, value, operation
):
    _, _, _, constructor = native
    monkeypatch.setattr(config.databases, field, value)
    with pytest.raises(DatabaseUnavailable):
        if operation == "get":
            store.get("session", "opaque")
        elif operation == "mutate":
            store.mutate([("session", "opaque")], lambda values: {})
        else:
            store.find("workspace", "session_id", "opaque")
    constructor.assert_not_called()


@pytest.mark.parametrize(
    "kind,identity",
    [
        ("deployment", "opaque"),
        ("session", "../secret"),
        ("session", ""),
        ("session", "x" * 129),
    ],
)
def test_invalid_kind_and_document_identity_are_rejected(native, kind, identity):
    _, _, _, constructor = native
    with pytest.raises(DatabaseUnavailable):
        store.get(kind, identity)
    constructor.assert_not_called()


def test_native_transform_reads_all_refs_before_set_or_delete(native):
    client, _, documents, _ = native
    store.get("workspace", "one")
    store.get("execution", "two")
    existing = {"state": "queued", "kind": "execution"}
    documents["execution_two"].get.return_value = SimpleNamespace(
        exists=True, to_dict=lambda: deepcopy(existing)
    )
    events = []
    for name, document in documents.items():
        snapshot = document.get.return_value
        document.get.side_effect = lambda name=name, snapshot=snapshot, **_: (
            events.append("read:" + name),
            snapshot,
        )[1]
    transaction = client.transaction.return_value
    transaction.set.side_effect = lambda *_: events.append("set")
    transaction.delete.side_effect = lambda *_: events.append("delete")

    def transform(values):
        assert values == {"workspace:one": None, "execution:two": existing}
        return {
            "workspace:one": {"kind": "workspace", "state": "active"},
            "execution:two": None,
        }

    store.mutate([("workspace", "one"), ("execution", "two")], transform)
    assert events == ["read:workspace_one", "read:execution_two", "set", "delete"]
    client.transaction.assert_called_once_with(max_attempts=3)
    for document in documents.values():
        assert document.get.call_args.kwargs == {
            "transaction": transaction,
            "timeout": 4,
            "retry": None,
        }
    transaction.delete.assert_called_once_with(documents["execution_two"])


def test_undeclared_mutation_cannot_write_arbitrary_control_document(native):
    client, _, _, _ = native
    with pytest.raises(DatabaseUnavailable):
        store.mutate(
            [("workspace", "one")],
            lambda values: {"control:active_policy": {"enabled": True}},
        )
    client.transaction.return_value.set.assert_not_called()


@pytest.mark.parametrize(
    "references",
    [
        [("session", "duplicate")] * 2,
        [("execution", str(index)) for index in range(33)],
    ],
)
def test_duplicate_or_unbounded_transaction_refs_are_rejected(native, references):
    _, _, _, constructor = native
    with pytest.raises(DatabaseUnavailable):
        store.mutate(references, lambda values: {})
    constructor.assert_not_called()


def test_native_noop_transform_does_not_write(native):
    client, _, _, _ = native
    store.mutate([("session", "opaque")], lambda values: {})
    client.transaction.return_value.set.assert_not_called()
    client.transaction.return_value.delete.assert_not_called()


def real_sdk_transaction(native, monkeypatch):
    """Exercise SDK transaction lifecycle against explicit RPC doubles."""
    client, collection, documents, _ = native
    client._database_string = "projects/database-test-project/databases/(default)"
    client._rpc_metadata = ()
    api = client._firestore_api
    api.begin_transaction.return_value = SimpleNamespace(transaction=b"first-cas-id")
    api.commit.return_value = SimpleNamespace(write_results=[], commit_time=None)
    transaction = Transaction(client, max_attempts=3)
    client.transaction.return_value = transaction
    monkeypatch.setattr(firestore, "transactional", transactional)
    make_document = collection.document.side_effect

    def document(name):
        reference = make_document(name)
        reference._document_path = f"{client._database_string}/documents/control/{name}"
        return reference

    collection.document.side_effect = document
    return client, api, transaction, documents


def test_real_sdk_atomic_commit_bounds_begin_and_commit_and_preserves_client(
    native, monkeypatch
):
    client, api, transaction, _ = real_sdk_transaction(native, monkeypatch)
    store.mutate(
        [("session", "opaque")],
        lambda values: {"session:opaque": {"kind": "session", "revoked_at": 123.5}},
    )
    for call in (api.begin_transaction.call_args, api.commit.call_args):
        assert call.kwargs["timeout"] == 4 and call.kwargs["retry"] is None
    request = api.commit.call_args.kwargs["request"]
    assert request["transaction"] == b"first-cas-id"
    assert len(request["writes"]) == 1
    assert request["writes"][0].update.fields["revoked_at"].double_value == 123.5
    assert not transaction.in_progress
    assert client._firestore_api is api
    assert transaction._client is not client
    api.rollback.assert_not_called()


def test_real_sdk_abort_retries_keep_cas_id_and_bound_every_transport_rpc(
    native, monkeypatch
):
    client, api, _, _ = real_sdk_transaction(native, monkeypatch)
    api.begin_transaction.side_effect = [
        SimpleNamespace(transaction=b"first-cas-id"),
        SimpleNamespace(transaction=b"retry-cas-id"),
    ]
    api.commit.side_effect = [
        Aborted("private-provider-diagnostic"),
        SimpleNamespace(write_results=[], commit_time=None),
    ]
    transform = Mock(return_value={})
    store.mutate([("session", "opaque")], transform)
    assert transform.call_count == 2
    retried_options = api.begin_transaction.call_args.kwargs["request"]["options"]
    assert retried_options.read_write.retry_transaction == b"first-cas-id"
    assert api.commit.call_args.kwargs["request"]["transaction"] == b"retry-cas-id"
    for name in ("begin_transaction", "commit"):
        calls = getattr(api, name).call_args_list
        assert len(calls) == 2
        assert all(call.kwargs["timeout"] == 4 for call in calls)
        assert all(call.kwargs["retry"] is None for call in calls)
    assert client._firestore_api is api


def test_real_sdk_authorization_denial_uses_bounded_rollback(native, monkeypatch):
    client, api, transaction, _ = real_sdk_transaction(native, monkeypatch)
    denial = HTTPException(403, "Database permission changed")

    def denied(values):
        raise denial

    with pytest.raises(HTTPException) as actual:
        store.mutate([("session", "opaque")], denied)
    assert actual.value is denial
    rollback = api.rollback.call_args.kwargs
    assert rollback["timeout"] == 4 and rollback["retry"] is None
    assert rollback["request"]["transaction"] == b"first-cas-id"
    assert not transaction.in_progress
    assert client._firestore_api is api
    api.commit.assert_not_called()


def test_real_sdk_commit_timeout_rolls_back_without_uncertain_commit_retry(
    native, monkeypatch
):
    _, api, transaction, _ = real_sdk_transaction(native, monkeypatch)
    api.commit.side_effect = DeadlineExceeded("private-provider-diagnostic")
    with pytest.raises(DatabaseUnavailable) as denied:
        store.mutate([("session", "opaque")], lambda values: {})
    assert str(denied.value) == "Database control store is unavailable"
    assert api.begin_transaction.call_count == api.commit.call_count == 1
    assert api.rollback.call_count == 1
    assert api.rollback.call_args.kwargs["timeout"] == 4
    assert api.rollback.call_args.kwargs["retry"] is None
    assert not transaction.in_progress


def test_application_authorization_denial_survives_transaction(native):
    client, _, _, _ = native
    denial = HTTPException(403, "Database permission changed")

    def denied(values):
        raise denial

    with pytest.raises(HTTPException) as actual:
        store.mutate([("session", "opaque")], denied)
    assert actual.value is denial
    client.transaction.return_value.set.assert_not_called()


@pytest.mark.parametrize("operation", ["get", "mutate", "find"])
def test_provider_failures_are_generic(native, monkeypatch, operation):
    monkeypatch.setattr(
        store, "_collection", Mock(side_effect=RuntimeError("SELECT private_value"))
    )
    with pytest.raises(DatabaseUnavailable) as denied:
        if operation == "get":
            store.get("session", "opaque")
        elif operation == "mutate":
            store.mutate([("session", "opaque")], lambda values: {})
        else:
            store.find("workspace", "session_id", "opaque")
    assert str(denied.value) == "Database control store is unavailable"


@pytest.mark.parametrize(
    "field,operator,value",
    [("session_id", "==", "opaque"), ("expires_at", "<=", 123.5)],
)
def test_native_find_scopes_kind_filter_expiry_comparison_and_bounded_stream(
    native, field, operator, value
):
    _, collection, _, _ = native
    collection.stream.return_value = [
        SimpleNamespace(to_dict=lambda: {"kind": "workspace"})
    ]
    assert store.find("workspace", field, value, limit=64) == [{"kind": "workspace"}]
    filters = [call.kwargs["filter"] for call in collection.where.call_args_list]
    assert [(item.field_path, item.op_string, item.value) for item in filters] == [
        ("kind", "==", "workspace"),
        (field, operator, value),
    ]
    collection.limit.assert_called_once_with(64)
    collection.stream.assert_called_once_with(timeout=4, retry=None)


@pytest.mark.parametrize(
    "field,limit", [("sql", 64), ("session_id", 0), ("session_id", 257)]
)
def test_lookup_field_and_cardinality_are_allowlisted(native, field, limit):
    _, _, _, constructor = native
    with pytest.raises(DatabaseUnavailable):
        store.find("workspace", field, "opaque", limit=limit)
    constructor.assert_not_called()


def test_explicit_adapter_restores_outer_scope_and_returns_detached_records(native):
    outer, inner = MemoryControlBackend(), MemoryControlBackend()
    with store.testing_backend(outer):
        store.mutate(
            [("session", "opaque")],
            lambda values: {
                "session:opaque": {
                    "kind": "session",
                    "login": "reader",
                    "state": "active",
                }
            },
        )
        record = store.get("session", "opaque")
        record["login"] = "other"
        assert store.get("session", "opaque")["login"] == "reader"
        with store.testing_backend(inner):
            assert store.get("session", "opaque") is None
        assert store.get("session", "opaque")["login"] == "reader"
        assert store.find("session", "state", "active") == [
            store.get("session", "opaque")
        ]
    assert store._test_backend is None
