"""Unit coverage for GitHub webhook authentication and replay journaling."""

from __future__ import annotations

import hashlib
import hmac
from copy import deepcopy
from unittest import mock

import pytest

from eng_platform_api.services import github_webhooks as webhooks


BODY = b'{"repository":{"full_name":"owner/repository"}}'


class _Snapshot:
    def __init__(self, value=None):
        self._value = deepcopy(value)
        self.exists = value is not None

    def to_dict(self):
        return deepcopy(self._value)


class _Transaction:
    def set(self, document, value):
        document.value = deepcopy(value)


class _Client:
    def transaction(self):
        return _Transaction()


class _Document:
    def __init__(self):
        self._client = _Client()
        self.value = None

    def get(self, transaction=None):
        return _Snapshot(self.value)

    def set(self, changes, merge=False):
        if merge and self.value is not None:
            self.value.update(deepcopy(changes))
        else:
            self.value = deepcopy(changes)


class _Collection:
    def __init__(self):
        self.documents = {}

    def document(self, key):
        return self.documents.setdefault(key, _Document())


@pytest.fixture
def firestore_collection(monkeypatch):
    from google.cloud import firestore

    collection = _Collection()
    monkeypatch.setattr(firestore, "transactional", lambda function: function)
    monkeypatch.setattr(webhooks, "_collection", lambda: collection)
    return collection


def test_verify_signature_accepts_only_exact_sha256_hmac(monkeypatch):
    monkeypatch.setattr(
        webhooks.config.release_orchestrator, "webhook_secret", "secret"
    )
    expected = "sha256=" + hmac.new(b"secret", BODY, hashlib.sha256).hexdigest()

    assert webhooks.verify_signature(BODY, expected) is True
    assert webhooks.verify_signature(BODY + b" ", expected) is False
    assert webhooks.verify_signature(BODY, "sha256=" + "0" * 64) is False
    assert webhooks.verify_signature(BODY, "") is False


def test_missing_secret_is_allowed_only_in_explicit_mock_mode(monkeypatch):
    monkeypatch.setattr(webhooks.config.release_orchestrator, "webhook_secret", "")
    monkeypatch.setattr(webhooks.config, "mock_mode", True)
    assert webhooks.verify_signature(BODY, "anything") is True

    monkeypatch.setattr(webhooks.config, "mock_mode", False)
    assert webhooks.verify_signature(BODY, "anything") is False


@pytest.mark.parametrize("delivery_id", ["", "x" * 129])
def test_receive_rejects_invalid_delivery_id(delivery_id):
    with pytest.raises(ValueError, match="Invalid GitHub delivery ID"):
        webhooks.receive(
            delivery_id=delivery_id,
            event="push",
            repository="owner/repository",
            body=BODY,
        )


@pytest.mark.parametrize("event", ["ping", "check_run", "issues", ""])
def test_receive_rejects_events_outside_minimal_allowlist(event):
    with pytest.raises(ValueError, match="Unsupported GitHub event"):
        webhooks.receive(
            delivery_id="delivery-1",
            event=event,
            repository="owner/repository",
            body=BODY,
        )


def test_receive_journals_hash_and_metadata_only():
    delivery, created = webhooks.receive(
        delivery_id="delivery-1",
        event="push",
        repository="owner/repository",
        body=BODY,
    )

    assert created is True
    assert delivery["delivery_id"] == "delivery-1"
    assert delivery["event"] == "push"
    assert delivery["repository"] == "owner/repository"
    assert delivery["payload_sha256"] == hashlib.sha256(BODY).hexdigest()
    assert delivery["status"] == "received"
    assert delivery["received_at"] == delivery["updated_at"]
    assert BODY.decode() not in repr(delivery)
    assert list(webhooks._memory) == [hashlib.sha256(b"delivery-1").hexdigest()]


@pytest.mark.parametrize("event", sorted(webhooks.ALLOWED_EVENTS))
def test_every_allowed_event_can_be_received(event):
    delivery, created = webhooks.receive(
        delivery_id=f"delivery-{event}",
        event=event,
        repository="owner/repository",
        body=BODY + event.encode(),
    )
    assert created is True
    assert delivery["event"] == event


def test_exact_redelivery_is_idempotent_and_returns_defensive_copy():
    first, _ = webhooks.receive(
        delivery_id="delivery-1",
        event="push",
        repository="owner/repository",
        body=BODY,
    )
    duplicate, created = webhooks.receive(
        delivery_id="delivery-1",
        event="push",
        repository="owner/repository",
        body=BODY,
    )

    assert created is False
    assert duplicate == first
    duplicate["status"] = "tampered"
    key = hashlib.sha256(b"delivery-1").hexdigest()
    assert webhooks._memory[key]["status"] == "received"


def test_delivery_id_cannot_be_reused_with_changed_payload():
    webhooks.receive(
        delivery_id="delivery-1",
        event="push",
        repository="owner/repository",
        body=BODY,
    )

    with pytest.raises(ValueError, match="payload changed"):
        webhooks.receive(
            delivery_id="delivery-1",
            event="push",
            repository="owner/repository",
            body=BODY + b"changed",
        )


@pytest.mark.parametrize(
    ("event", "repository"),
    [
        ("workflow_run", "owner/repository"),
        ("push", "owner/other"),
    ],
)
def test_delivery_id_cannot_be_replayed_with_changed_routing_metadata(
    event, repository
):
    webhooks.receive(
        delivery_id="delivery-1",
        event="push",
        repository="owner/repository",
        body=BODY,
    )

    with pytest.raises(ValueError, match="payload changed"):
        webhooks.receive(
            delivery_id="delivery-1",
            event=event,
            repository=repository,
            body=BODY,
        )


def test_complete_marks_delivery_processed_and_records_sanitized_result():
    webhooks.receive(
        delivery_id="delivery-1",
        event="workflow_run",
        repository="owner/repository",
        body=BODY,
    )

    webhooks.complete("delivery-1", outcome="accepted", execution_ids=["execution-1"])

    key = hashlib.sha256(b"delivery-1").hexdigest()
    stored = webhooks._memory[key]
    assert stored["status"] == "processed"
    assert stored["outcome"] == "accepted"
    assert stored["execution_ids"] == ["execution-1"]
    assert stored["updated_at"] >= stored["received_at"]


def test_complete_forces_processed_status_even_if_caller_supplies_status():
    webhooks.receive(
        delivery_id="delivery-1",
        event="push",
        repository="owner/repository",
        body=BODY,
    )

    webhooks.complete("delivery-1", status="attacker-controlled")

    key = hashlib.sha256(b"delivery-1").hexdigest()
    assert webhooks._memory[key]["status"] == "processed"


def test_complete_unknown_delivery_raises_key_error():
    with pytest.raises(KeyError, match="missing"):
        webhooks.complete("missing", outcome="ignored")


def test_collection_uses_configured_firestore_project_and_name(monkeypatch):
    from google.cloud import firestore

    client = mock.Mock()
    sentinel = object()
    client.return_value.collection.return_value = sentinel
    monkeypatch.setattr(firestore, "Client", client)
    monkeypatch.setattr(webhooks.config, "mock_mode", False)
    monkeypatch.setattr(webhooks.config.release_orchestrator, "enabled", True)
    monkeypatch.setattr(
        webhooks.config.release_orchestrator,
        "webhook_delivery_collection",
        "deliveries-test",
    )
    monkeypatch.setattr(webhooks.config.cloud_build, "project_id", "build-project")

    assert webhooks._collection() is sentinel
    client.assert_called_once_with(project="build-project")
    client.return_value.collection.assert_called_once_with("deliveries-test")


@pytest.mark.parametrize(
    ("mock_mode", "enabled", "collection_name"),
    [(True, True, "items"), (False, False, "items"), (False, True, "")],
)
def test_collection_is_disabled_without_explicit_production_configuration(
    monkeypatch, mock_mode, enabled, collection_name
):
    monkeypatch.setattr(webhooks.config, "mock_mode", mock_mode)
    monkeypatch.setattr(webhooks.config.release_orchestrator, "enabled", enabled)
    monkeypatch.setattr(
        webhooks.config.release_orchestrator,
        "webhook_delivery_collection",
        collection_name,
    )
    assert webhooks._collection() is None


def test_firestore_receive_redelivery_conflict_and_complete(firestore_collection):
    first, created = webhooks.receive(
        delivery_id="delivery-1",
        event="push",
        repository="owner/repository",
        body=BODY,
    )
    duplicate, duplicate_created = webhooks.receive(
        delivery_id="delivery-1",
        event="push",
        repository="owner/repository",
        body=BODY,
    )

    assert created is True
    assert duplicate_created is False
    assert duplicate == first
    with pytest.raises(ValueError, match="payload changed"):
        webhooks.receive(
            delivery_id="delivery-1",
            event="workflow_run",
            repository="owner/repository",
            body=BODY,
        )

    webhooks.complete("delivery-1", outcome="accepted")
    stored = firestore_collection.document(webhooks._id("delivery-1")).value
    assert stored["status"] == "processed"
    assert stored["outcome"] == "accepted"
