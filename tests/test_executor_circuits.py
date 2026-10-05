"""Unit coverage for the persistent account-level Actions circuit breaker."""

from __future__ import annotations

from copy import deepcopy
from unittest import mock

import pytest

from eng_platform_api.services import executor_circuits as circuits


OWNER = "diegomad14"


class _Snapshot:
    def __init__(self, value=None):
        self._value = deepcopy(value)
        self.exists = value is not None

    def to_dict(self):
        return deepcopy(self._value)


class _Transaction:
    def set(self, document, value):
        document.value = deepcopy(value)

    def update(self, document, changes):
        document.value.update(deepcopy(changes))


class _Client:
    def transaction(self):
        return _Transaction()


class _Document:
    def __init__(self):
        self._client = _Client()
        self.value = None

    def get(self, transaction=None):
        return _Snapshot(self.value)


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
    monkeypatch.setattr(circuits, "_collection", lambda: collection)
    return collection


def _open(**overrides):
    values = {
        "reason": "github_actions_billing_rejection",
        "repository": "diegomad14/private-repo",
        "run_id": "42",
        "evidence": "recent account payments have failed",
    }
    values.update(overrides)
    return circuits.open_circuit(OWNER, **values)


def test_unknown_owner_defaults_to_closed_without_persisting_state():
    value = circuits.get(OWNER)

    assert value == {"owner": OWNER, "state": "closed", "updated_at": ""}
    assert circuits.is_open(OWNER) is False
    assert circuits._memory == {}


def test_owner_identity_is_case_and_whitespace_insensitive():
    opened, created = circuits.open_circuit("  DiegoMad14 ", reason="billing")

    assert created is True
    assert circuits.is_open("diegomad14") is True
    assert circuits.get("DIEGOMAD14")["reason"] == "billing"
    assert opened["owner"] == "  DiegoMad14 "


def test_open_circuit_records_sanitized_evidence_once(monkeypatch):
    monkeypatch.setattr(circuits, "_now", lambda: "2026-09-22T12:00:00+00:00")

    opened, created = _open(reason="r" * 300, evidence="e" * 1200, run_id=99)
    duplicate, duplicate_created = _open(reason="replacement")

    assert created is True
    assert duplicate_created is False
    assert len(opened["reason"]) == 256
    assert len(opened["evidence"]) == 1000
    assert opened["run_id"] == "99"
    assert opened["opened_at"] == "2026-09-22T12:00:00+00:00"
    assert duplicate["reason"] == "r" * 256


def test_get_returns_a_defensive_copy():
    opened, _ = _open()
    opened["state"] = "tampered"
    fetched = circuits.get(OWNER)
    fetched["state"] = "also-tampered"

    assert circuits.is_open(OWNER) is True


def test_request_probe_requires_open_circuit():
    with pytest.raises(ValueError, match="circuit is not open"):
        circuits.request_probe(
            OWNER,
            repository="diegomad14/private-repo",
            workflow="eng-platform-actions-health.yml",
            requested_by="admin",
        )


def test_request_probe_binds_repository_workflow_and_requester(monkeypatch):
    _open()
    monkeypatch.setattr(circuits.secrets, "token_urlsafe", lambda size: "nonce")
    monkeypatch.setattr(circuits, "_now", lambda: "2026-09-22T12:01:00+00:00")

    circuit = circuits.request_probe(
        OWNER,
        repository="diegomad14/private-repo",
        workflow="eng-platform-actions-health.yml",
        requested_by="diegomad14",
    )

    assert circuit["state"] == "open"
    assert circuit["probe"] == {
        "status": "requested",
        "nonce": "nonce",
        "repository": "diegomad14/private-repo",
        "workflow": "eng-platform-actions-health.yml",
        "requested_by": "diegomad14",
        "requested_at": "2026-09-22T12:01:00+00:00",
    }


def test_a_new_probe_request_replaces_an_unverified_probe(monkeypatch):
    _open()
    nonces = iter(("first", "second"))
    monkeypatch.setattr(circuits.secrets, "token_urlsafe", lambda size: next(nonces))

    circuits.request_probe(
        OWNER, repository="owner/one", workflow="health.yml", requested_by="admin"
    )
    updated = circuits.request_probe(
        OWNER, repository="owner/two", workflow="health.yml", requested_by="admin"
    )

    assert updated["probe"]["nonce"] == "second"
    assert updated["probe"]["repository"] == "owner/two"


@pytest.mark.parametrize(
    ("repository", "workflow"),
    [
        ("owner/other", "health.yml"),
        ("owner/private", "other.yml"),
    ],
)
def test_record_probe_rejects_mismatched_request(repository, workflow):
    _open()
    circuits.request_probe(
        OWNER,
        repository="owner/private",
        workflow="health.yml",
        requested_by="admin",
    )

    with pytest.raises(ValueError, match="does not match"):
        circuits.record_probe(
            OWNER,
            repository=repository,
            workflow=workflow,
            run_id="123",
            conclusion="success",
            jobs_started=1,
        )


def test_record_probe_requires_requested_open_probe():
    _open()
    with pytest.raises(ValueError, match="does not match"):
        circuits.record_probe(
            OWNER,
            repository="owner/private",
            workflow="health.yml",
            run_id="123",
            conclusion="success",
            jobs_started=1,
        )


def test_record_probe_preserves_request_and_records_verified_result(monkeypatch):
    _open()
    monkeypatch.setattr(circuits.secrets, "token_urlsafe", lambda size: "nonce")
    circuits.request_probe(
        OWNER,
        repository="owner/private",
        workflow="health.yml",
        requested_by="admin",
    )
    monkeypatch.setattr(circuits, "_now", lambda: "2026-09-22T12:02:00+00:00")

    circuit = circuits.record_probe(
        OWNER,
        repository="owner/private",
        workflow="health.yml",
        run_id=123,
        conclusion="success",
        jobs_started="2",
    )

    assert circuit["probe"]["status"] == "verified"
    assert circuit["probe"]["nonce"] == "nonce"
    assert circuit["probe"]["requested_by"] == "admin"
    assert circuit["probe"]["run_id"] == "123"
    assert circuit["probe"]["jobs_started"] == 2
    assert circuit["probe"]["completed_at"] == "2026-09-22T12:02:00+00:00"


@pytest.mark.parametrize(
    ("conclusion", "jobs_started", "run_id"),
    [
        ("failure", 1, "123"),
        ("success", 0, "123"),
        ("success", 1, "other"),
    ],
)
def test_close_requires_matching_successful_started_probe(
    conclusion, jobs_started, run_id
):
    _open()
    circuits.request_probe(
        OWNER,
        repository="owner/private",
        workflow="health.yml",
        requested_by="admin",
    )
    circuits.record_probe(
        OWNER,
        repository="owner/private",
        workflow="health.yml",
        run_id="123",
        conclusion=conclusion,
        jobs_started=jobs_started,
    )

    with pytest.raises(ValueError, match="successful started health probe"):
        circuits.close_after_successful_probe(OWNER, run_id=run_id)
    assert circuits.is_open(OWNER) is True


def test_close_requires_existing_verified_probe():
    with pytest.raises(ValueError, match="does not exist"):
        circuits.close_after_successful_probe(OWNER, run_id="123")

    _open()
    with pytest.raises(ValueError, match="successful started health probe"):
        circuits.close_after_successful_probe(OWNER, run_id="123")


def test_successful_started_probe_is_the_only_close_path(monkeypatch):
    _open()
    circuits.request_probe(
        OWNER,
        repository="owner/private",
        workflow="health.yml",
        requested_by="admin",
    )
    circuits.record_probe(
        OWNER,
        repository="owner/private",
        workflow="health.yml",
        run_id="123",
        conclusion="success",
        jobs_started=1,
    )
    monkeypatch.setattr(circuits, "_now", lambda: "2026-09-22T12:03:00+00:00")

    closed = circuits.close_after_successful_probe(OWNER, run_id=123)

    assert closed["state"] == "closed"
    assert closed["closed_at"] == "2026-09-22T12:03:00+00:00"
    assert closed["close_reason"] == "successful_health_probe"
    assert circuits.is_open(OWNER) is False
    with pytest.raises(ValueError, match="successful started health probe"):
        circuits.close_after_successful_probe(OWNER, run_id="123")


def test_closed_circuit_can_be_reopened_with_fresh_evidence():
    _open()
    circuits.request_probe(
        OWNER,
        repository="owner/private",
        workflow="health.yml",
        requested_by="admin",
    )
    circuits.record_probe(
        OWNER,
        repository="owner/private",
        workflow="health.yml",
        run_id="123",
        conclusion="success",
        jobs_started=1,
    )
    circuits.close_after_successful_probe(OWNER, run_id="123")

    reopened, created = circuits.open_circuit(
        OWNER, reason="new billing failure", run_id="456"
    )

    assert created is True
    assert reopened["state"] == "open"
    assert reopened["run_id"] == "456"
    assert "probe" not in reopened


def test_collection_uses_monitoring_project_when_build_project_is_absent(monkeypatch):
    from google.cloud import firestore

    client = mock.Mock()
    sentinel = object()
    client.return_value.collection.return_value = sentinel
    monkeypatch.setattr(firestore, "Client", client)
    monkeypatch.setattr(circuits.config, "mock_mode", False)
    monkeypatch.setattr(circuits.config.release_orchestrator, "enabled", True)
    monkeypatch.setattr(
        circuits.config.release_orchestrator,
        "circuit_collection",
        "circuits-test",
    )
    monkeypatch.setattr(circuits.config.cloud_build, "project_id", "")
    monkeypatch.setattr(
        circuits.config.monitoring, "gcp_project_id", "monitoring-project"
    )

    assert circuits._collection() is sentinel
    client.assert_called_once_with(project="monitoring-project")
    client.return_value.collection.assert_called_once_with("circuits-test")


@pytest.mark.parametrize(
    ("mock_mode", "enabled", "collection_name"),
    [(True, True, "items"), (False, False, "items"), (False, True, "")],
)
def test_collection_is_disabled_without_explicit_production_configuration(
    monkeypatch, mock_mode, enabled, collection_name
):
    monkeypatch.setattr(circuits.config, "mock_mode", mock_mode)
    monkeypatch.setattr(circuits.config.release_orchestrator, "enabled", enabled)
    monkeypatch.setattr(
        circuits.config.release_orchestrator, "circuit_collection", collection_name
    )
    assert circuits._collection() is None


def test_firestore_open_get_and_idempotent_reopen(firestore_collection):
    assert circuits.get(OWNER)["state"] == "closed"

    opened, created = _open()
    duplicate, duplicate_created = _open(reason="replacement")

    assert created is True
    assert duplicate_created is False
    assert duplicate == opened
    assert circuits.get(OWNER) == opened

    firestore_collection.document(circuits._id(OWNER)).value["state"] = "closed"
    reopened, reopened_created = _open(reason="new failure")
    assert reopened_created is True
    assert reopened["state"] == "open"
    assert reopened["reason"] == "new failure"


def test_firestore_probe_lifecycle_closes_only_after_verified_success(
    firestore_collection, monkeypatch
):
    _open()
    monkeypatch.setattr(circuits.secrets, "token_urlsafe", lambda size: "nonce")

    requested = circuits.request_probe(
        OWNER,
        repository="owner/private",
        workflow="health.yml",
        requested_by="admin",
    )
    verified = circuits.record_probe(
        OWNER,
        repository="owner/private",
        workflow="health.yml",
        run_id="123",
        conclusion="success",
        jobs_started=1,
    )
    closed = circuits.close_after_successful_probe(OWNER, run_id="123")

    assert requested["probe"]["nonce"] == "nonce"
    assert verified["probe"]["status"] == "verified"
    assert closed["state"] == "closed"
    assert firestore_collection.document(circuits._id(OWNER)).value["state"] == "closed"


def test_firestore_request_probe_rechecks_state_inside_transaction(
    firestore_collection, monkeypatch
):
    monkeypatch.setattr(circuits, "get", lambda owner: {"state": "open"})
    with pytest.raises(ValueError, match="circuit is not open"):
        circuits.request_probe(
            OWNER,
            repository="owner/private",
            workflow="health.yml",
            requested_by="admin",
        )


def test_firestore_record_probe_rechecks_document_and_identity(
    firestore_collection, monkeypatch
):
    requested = {
        "status": "requested",
        "repository": "owner/private",
        "workflow": "health.yml",
    }
    monkeypatch.setattr(
        circuits, "get", lambda owner: {"state": "open", "probe": requested}
    )
    with pytest.raises(ValueError, match="does not exist"):
        circuits.record_probe(
            OWNER,
            repository="owner/private",
            workflow="health.yml",
            run_id="123",
            conclusion="success",
            jobs_started=1,
        )

    firestore_collection.document(circuits._id(OWNER)).value = {
        "state": "open",
        "probe": {**requested, "workflow": "different.yml"},
    }
    with pytest.raises(ValueError, match="does not match"):
        circuits.record_probe(
            OWNER,
            repository="owner/private",
            workflow="health.yml",
            run_id="123",
            conclusion="success",
            jobs_started=1,
        )


def test_firestore_close_rejects_missing_or_unsuccessful_probe(firestore_collection):
    with pytest.raises(ValueError, match="does not exist"):
        circuits.close_after_successful_probe(OWNER, run_id="123")

    firestore_collection.document(circuits._id(OWNER)).value = {
        "state": "open",
        "probe": {
            "status": "verified",
            "run_id": "123",
            "conclusion": "failure",
            "jobs_started": 1,
        },
    }
    with pytest.raises(ValueError, match="successful started health probe"):
        circuits.close_after_successful_probe(OWNER, run_id="123")


def test_memory_probe_operations_recheck_state_after_initial_read(monkeypatch):
    requested = {
        "status": "requested",
        "repository": "owner/private",
        "workflow": "health.yml",
    }
    monkeypatch.setattr(
        circuits, "get", lambda owner: {"state": "open", "probe": requested}
    )

    with pytest.raises(ValueError, match="circuit is not open"):
        circuits.request_probe(
            OWNER,
            repository="owner/private",
            workflow="health.yml",
            requested_by="admin",
        )
    with pytest.raises(ValueError, match="circuit is not open"):
        circuits.record_probe(
            OWNER,
            repository="owner/private",
            workflow="health.yml",
            run_id="123",
            conclusion="success",
            jobs_started=1,
        )


@pytest.mark.parametrize("durable", [False, True])
def test_mode_propagation_claim_and_cooldown_are_shared(monkeypatch, request, durable):
    if durable:
        collection = request.getfixturevalue("firestore_collection")
        document = collection.document(circuits._id(OWNER))
    now = [1000.0]
    monkeypatch.setattr(circuits.time, "time", lambda: now[0])
    assert circuits.claim_mode_propagation(OWNER) is None
    assert circuits.mode_propagation_due(circuits.get(OWNER)) is False
    original, _ = _open()
    assert circuits.mode_propagation_due(original) is True
    token = circuits.claim_mode_propagation(OWNER)
    assert token
    assert circuits.claim_mode_propagation(OWNER) is None
    assert circuits.mode_propagation_due(circuits.get(OWNER)) is False
    circuits.finish_mode_propagation(OWNER, token=token, succeeded=True)
    finished = circuits.get(OWNER)
    assert {key: finished[key] for key in original} == original
    assert finished["mode_propagation"]["retry_after"] == 1600.0
    # Reads during the cooldown do not update any shared metadata.
    if durable:
        stored = deepcopy(document.value)
    now[0] = 1599.0
    assert circuits.claim_mode_propagation(OWNER) is None
    if durable:
        assert document.value == stored
    now[0] = 1600.0
    assert circuits.claim_mode_propagation(OWNER)


@pytest.mark.parametrize("durable", [False, True])
def test_mode_propagation_partial_failure_and_crash_are_retryable(
    monkeypatch, request, durable
):
    if durable:
        request.getfixturevalue("firestore_collection")
    now = [1000.0]
    monkeypatch.setattr(circuits.time, "time", lambda: now[0])
    _open()
    token = circuits.claim_mode_propagation(OWNER)
    circuits.finish_mode_propagation(OWNER, token=token, succeeded=False)
    now[0] = 1059.0
    assert circuits.claim_mode_propagation(OWNER) is None
    now[0] = 1060.0
    abandoned = circuits.claim_mode_propagation(OWNER)
    assert abandoned and abandoned != token
    # No completion call: a different worker can recover the expired lease.
    now[0] = 1359.0
    assert circuits.claim_mode_propagation(OWNER) is None
    now[0] = 1360.0
    recovered = circuits.claim_mode_propagation(OWNER)
    assert recovered and recovered != abandoned
    # A delayed worker must not clear or extend the successor's claim.
    current = circuits.get(OWNER)
    circuits.finish_mode_propagation(OWNER, token=abandoned, succeeded=True)
    assert circuits.get(OWNER) == current


@pytest.mark.parametrize("durable", [False, True])
def test_mode_propagation_never_overwrites_closed_or_reopened_circuit(
    monkeypatch, request, durable
):
    if durable:
        request.getfixturevalue("firestore_collection")
    monkeypatch.setattr(circuits.time, "time", lambda: 1000.0)
    _open()
    token = circuits.claim_mode_propagation(OWNER)
    circuits.request_probe(
        OWNER, repository="owner/repo", workflow="health.yml", requested_by="admin"
    )
    verified = circuits.record_probe(
        OWNER,
        repository="owner/repo",
        workflow="health.yml",
        run_id="1",
        conclusion="success",
        jobs_started=1,
    )
    circuits.finish_mode_propagation(OWNER, token=token, succeeded=True)
    assert circuits.get(OWNER)["probe"] == verified["probe"]
    closed = circuits.close_after_successful_probe(OWNER, run_id="1")
    circuits.finish_mode_propagation(OWNER, token=token, succeeded=False)
    assert circuits.get(OWNER) == closed
    assert circuits.claim_mode_propagation(OWNER) is None
    reopened, _ = _open(reason="new failure")
    assert "mode_propagation" not in reopened
    new_token = circuits.claim_mode_propagation(OWNER)
    assert new_token and new_token != token
    current = circuits.get(OWNER)
    circuits.finish_mode_propagation(OWNER, token=token, succeeded=False)
    assert circuits.get(OWNER) == current


def test_mode_propagation_concurrent_claims_have_one_winner():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _open()
    barrier = Barrier(8)

    def claim(_):
        barrier.wait(timeout=5)
        return circuits.claim_mode_propagation(OWNER)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(claim, range(8)))
    assert sum(token is not None for token in claims) == 1


def test_finishing_unknown_propagation_does_not_create_circuit(firestore_collection):
    circuits.finish_mode_propagation(OWNER, token="unknown", succeeded=True)
    assert firestore_collection.document(circuits._id(OWNER)).value is None


def test_durable_concurrent_claims_use_shared_state_without_per_event_writes(
    monkeypatch, firestore_collection
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, RLock

    from google.cloud import firestore

    # Model Firestore's serializable transaction retry/commit boundary. The
    # durable path must coordinate through the document, not a process-local cache.
    transaction_lock = RLock()

    def transactional(function):
        def execute(transaction):
            with transaction_lock:
                return function(transaction)

        return execute

    monkeypatch.setattr(firestore, "transactional", transactional)
    writes = []
    original_update = _Transaction.update

    def update(transaction, document, changes):
        writes.append(deepcopy(changes))
        original_update(transaction, document, changes)

    monkeypatch.setattr(_Transaction, "update", update)
    _open()
    barrier = Barrier(8)

    def claim(_):
        barrier.wait(timeout=5)
        return circuits.claim_mode_propagation(OWNER)

    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(claim, range(8)))
    winners = [token for token in claims if token is not None]
    assert len(winners) == 1
    assert len(writes) == 1
    circuits.finish_mode_propagation(OWNER, token=winners[0], succeeded=True)
    for _ in range(20):
        assert circuits.claim_mode_propagation(OWNER) is None
    assert len(writes) == 2
    assert circuits._memory == {}


def test_malformed_durable_propagation_metadata_can_be_repaired(
    monkeypatch, firestore_collection
):
    monkeypatch.setattr(circuits.time, "time", lambda: 1000.0)
    _open()
    document = firestore_collection.document(circuits._id(OWNER))
    document.value["mode_propagation"] = "invalid"
    circuits.finish_mode_propagation(OWNER, token="old", succeeded=True)
    assert document.value["mode_propagation"] == "invalid"
    token = circuits.claim_mode_propagation(OWNER)
    assert token
    circuits.finish_mode_propagation(OWNER, token=token, succeeded=True)
    assert document.value["mode_propagation"]["retry_after"] == 1600.0
    assert document.value["state"] == "open"
