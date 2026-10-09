"""Global bootstrap fencing, Firestore atomicity and stale-provider admission."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
from threading import RLock
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from eng_platform_api.config import config
from eng_platform_api.routers import release_execution_events as events
from eng_platform_api.models import ReleaseExecutionEvent
from eng_platform_api.services import release_executions as store
from eng_platform_api.services import release_orchestrator as orchestrator

BASE = "3c807a789ba78eea95068a3cc0a7a6e537b0bae9"
HEAD = "a54d1fef459f74367001e824a29dcc6a68dd0d52"


class Snapshot:
    def __init__(self, value):
        self.exists = value is not None
        self.value = deepcopy(value)

    def to_dict(self):
        return deepcopy(self.value)


class Transaction:
    def __init__(self, collection):
        self.collection = collection
        self.writes = []

    def update(self, document, value):
        self.writes.append((document.key, deepcopy(value), True))

    def set(self, document, value):
        self.writes.append((document.key, deepcopy(value), False))

    def commit(self):
        if self.collection.fail_commit:
            raise RuntimeError("offline persistence unavailable")
        staged = deepcopy(self.collection.values)
        for key, value, merge in self.writes:
            if merge:
                staged[key].update(value)
            else:
                staged[key] = value
        self.collection.values = staged


class Document:
    def __init__(self, key, collection):
        self.key = key
        self.collection = collection
        self._client = collection

    def get(self, transaction=None):
        return Snapshot(self.collection.values.get(self.key))


class Collection:
    def __init__(self):
        self.values = {}
        self.lock = RLock()
        self.fail_commit = False

    def document(self, key):
        return Document(key, self)

    def transaction(self):
        return Transaction(self)

    def stream(self):
        return [Snapshot(value) for value in self.values.values()]

    def where(self, field, operator, value):
        assert operator == "=="
        return SimpleNamespace(
            stream=lambda: [
                Snapshot(item)
                for item in self.values.values()
                if item.get(field) == value
            ]
        )


@pytest.fixture(params=["memory", "firestore"])
def backend(request, monkeypatch):
    if request.param == "memory":
        monkeypatch.setattr(store, "_collection", lambda: None)
        return None
    from google.cloud import firestore

    collection = Collection()

    def transactional(function):
        def execute(transaction):
            with collection.lock:
                result = function(transaction)
                transaction.commit()
                return result

        return execute

    monkeypatch.setattr(firestore, "transactional", transactional)
    monkeypatch.setattr(store, "_collection", lambda: collection)
    return collection


def execution(head=HEAD):
    identity = dict(
        repository="diegomad14/cgm-artemis-api",
        service_name="cgm-artemis-api",
        operation="pr_quality",
        head_sha=head,
        base_sha=BASE,
        profile_hash="a" * 64,
        executor_digest="quality@sha256:" + "b" * 64,
        policy_hash="c" * 64,
    )
    value, _ = store.reserve(
        fingerprint_value=store.fingerprint(**identity),
        branch="fix/isolation",
        **identity,
    )
    return store.save(value["execution_id"], pull_request_number=172)


def binding(value, key="key", nonce="offline-unique-attempt"):
    request = {"timeout": "3600s", "substitutions": {"_BOOTSTRAP_NONCE": nonce}}
    return {
        **{
            key: value[key]
            for key in (
                "repository",
                "service_name",
                "head_sha",
                "base_sha",
                "fingerprint",
                "profile_hash",
                "executor_digest",
                "policy_hash",
            )
        },
        "requested_by": "diegomad14",
        "idempotency_key": hashlib.sha256(key.encode()).hexdigest(),
        "repository_id": 1306114845,
        "pull_request_number": 172,
        "authorization_policy": store.QUALITY_BOOTSTRAP_TICKET_ID,
        "build_request": request,
        "build_request_hash": hashlib.sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "nonce": nonce,
        "dispatch_token_hash": hashlib.sha256(b"offline-dispatch-permit").hexdigest(),
        "max_compute_usd": "0.36",
        "timeout_seconds": 3600,
    }


def reserve(value, **kwargs):
    return store.reserve_quality_bootstrap(
        value["execution_id"], binding=binding(value, **kwargs)
    )


def verified_build(value):
    assert store.claim_quality_bootstrap_dispatch(
        value["execution_id"],
        attempt_nonce=value["bootstrap_nonce"],
        dispatch_token="offline-dispatch-permit",
    )
    return store.update_quality_bootstrap(
        value["execution_id"],
        attempt_nonce=value["bootstrap_nonce"],
        changes={
            "build_id": "sole-build",
            "provider_run_id": "sole-build",
            "bootstrap_build_verified": True,
            "bootstrap_verified_request_hash": value["bootstrap_build_request_hash"],
            "status": "running_quality",
        },
    )


def test_dispatch_permit_is_ephemeral_and_consumed_once(backend):
    value, _ = reserve(execution())
    assert not store.claim_quality_bootstrap_dispatch(
        value["execution_id"],
        attempt_nonce=value["bootstrap_nonce"],
        dispatch_token="wrong-permit",
    )
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(
            pool.map(
                lambda _: store.claim_quality_bootstrap_dispatch(
                    value["execution_id"],
                    attempt_nonce=value["bootstrap_nonce"],
                    dispatch_token="offline-dispatch-permit",
                ),
                range(8),
            )
        )
    assert sum(claims) == 1
    with pytest.raises(ValueError):
        store.save(value["execution_id"], bootstrap_dispatch_started=False)
    with pytest.raises(ValueError):
        store.update_quality_bootstrap(
            value["execution_id"],
            attempt_nonce=value["bootstrap_nonce"],
            changes={"bootstrap_dispatch_started": False},
        )
    assert "offline-dispatch-permit" not in json.dumps(
        store.get_quality_bootstrap_ticket()
    )


def test_global_ticket_has_one_winner_across_keys_and_heads(backend):
    candidates = [execution(f"{number:040x}") for number in range(1, 17)]

    def attempt(pair):
        index, value = pair
        try:
            return reserve(value, key=f"key-{index}")
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, enumerate(candidates)))
    winners = [result[0] for result in results if result is not None and result[1]]
    assert len(winners) == 1
    winner = winners[0]
    assert winner["provider"] == "cloud_build"
    assert winner["status"] == "submitting"
    assert winner["bootstrap_attempts"] == 1
    ticket = store.get_quality_bootstrap_ticket()
    assert ticket["execution_id"] == winner["execution_id"]
    assert all(
        not store.claim_submission(value["execution_id"]) for value in candidates
    )


def test_same_key_returns_state_without_another_reservation(backend):
    value = execution()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: reserve(value), range(8)))
    assert sum(won for _, won in results) == 1
    store.update_quality_bootstrap(
        value["execution_id"],
        attempt_nonce="offline-unique-attempt",
        changes={"status": "unknown"},
    )
    current, won = reserve(value)
    assert not won and current["status"] == "unknown"
    with pytest.raises(ValueError, match="consumed"):
        reserve(value, key="new-key")
    assert not store.claim_submission(value["execution_id"])


def test_same_key_cannot_return_a_different_execution_or_binding(backend):
    value = execution()
    reserve(value)
    other = execution("e" * 40)
    with pytest.raises(ValueError, match="consumed"):
        reserve(other)
    changed = binding(value)
    changed["profile_hash"] = "e" * 64
    with pytest.raises(ValueError, match="consumed"):
        store.reserve_quality_bootstrap(value["execution_id"], binding=changed)


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider_run_id", "42"),
        ("github_run_id", 42),
        ("build_id", "build"),
        ("source_token_issued_at", "timestamp"),
        ("source_token_build_id", "build"),
        ("event_token_hash", "d" * 64),
        ("event_token_issued_at", "timestamp"),
        ("event_token_build_id", "build"),
        ("event_token_provider_run_id", "42"),
        ("event_sequence", 1),
        ("pending_report_hash", "d" * 64),
        ("report_hash", "d" * 64),
        ("evidence_committed", True),
        ("event_admitted_at", "timestamp"),
        ("status", "failed"),
    ],
)
def test_prior_admission_evidence_or_terminal_status_rejects_ticket(
    backend, field, value
):
    current = execution()
    store.save(current["execution_id"], **{field: value})
    with pytest.raises(ValueError, match="fresh"):
        reserve(current)
    assert store.get_quality_bootstrap_ticket() is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("repository", "other/repo"),
        ("repository_id", 1),
        ("service_name", "other"),
        ("pull_request_number", 173),
        ("base_sha", "e" * 40),
        ("head_sha", "e" * 40),
        ("fingerprint", "e" * 64),
        ("profile_hash", "e" * 64),
        ("executor_digest", "quality@sha256:" + "e" * 64),
        ("policy_hash", "e" * 64),
        ("timeout_seconds", 3601),
        ("max_compute_usd", "0.37"),
        ("authorization_policy", "other-policy"),
        ("build_request_hash", "e" * 64),
    ],
)
def test_binding_drift_rejected_before_consuming_ticket(backend, field, value):
    current = execution()
    proposed = binding(current)
    proposed[field] = value
    with pytest.raises(ValueError):
        store.reserve_quality_bootstrap(current["execution_id"], binding=proposed)
    assert store.get_quality_bootstrap_ticket() is None


@pytest.mark.parametrize(
    "changes",
    [
        {"bootstrap_nonce": "new"},
        {"bootstrap_attempts": 0},
        {"bootstrap_ticket_id": ""},
        {"bootstrap_build_request": {}},
        {"provider": "github_actions"},
        {"build_id": "new-build"},
        {"provider_run_id": "new-run"},
        {"event_token_hash": "e" * 64},
        {"previous_build_ids": ["previous"]},
        {"status": "waiting_github"},
        {"status": "submission_pending"},
        {"status": "submission_failed"},
        {"planner_retry_count": 0},
        {"pull_request_number": 173},
        {"repository_id": 1},
    ],
)
def test_generic_saves_cannot_reopen_or_mutate_ticket(backend, changes):
    current, _ = reserve(execution())
    with pytest.raises(ValueError):
        store.save(current["execution_id"], **changes)
    assert store.get(current["execution_id"])["bootstrap_attempts"] == 1
    assert store.get_quality_bootstrap_ticket()["nonce"] == "offline-unique-attempt"


def test_all_retry_paths_remain_closed_after_uncertainty(backend):
    value, _ = reserve(execution())
    identity = value["execution_id"]
    store.update_quality_bootstrap(
        identity, attempt_nonce=value["bootstrap_nonce"], changes={"status": "unknown"}
    )
    assert not store.claim_submission(identity)
    assert not store.planner_retry_candidate(value)
    with pytest.raises(ValueError):
        store.reconcile_submission_absent(identity)
    with pytest.raises(ValueError):
        store.transition_to_cloud_build(identity, reason="billing")
    with pytest.raises(ValueError):
        store.stage_planner_retry(
            identity,
            failed_build_id="build",
            planner_image="planner@sha256:" + "e" * 64,
        )
    with pytest.raises(ValueError):
        store.bind_build(identity, build_id="unverified")
    with pytest.raises(ValueError):
        store.update_quality_bootstrap(
            identity,
            attempt_nonce=value["bootstrap_nonce"],
            changes={"status": "submission_pending"},
        )


def test_tokens_require_verified_single_build_and_are_one_shot(backend):
    value, _ = reserve(execution())
    identity = value["execution_id"]
    assert not store.claim_source_token(identity, provider_run_id="sole-build")
    assert not store.claim_event_token(
        identity, provider_run_id="sole-build", token_hash="e" * 64
    )
    current = verified_build(value)
    assert not store.claim_source_token(identity, provider_run_id="other-build")
    assert store.claim_source_token(identity, provider_run_id="sole-build")
    assert not store.claim_source_token(identity, provider_run_id="sole-build")
    assert store.claim_event_token(
        identity, provider_run_id="sole-build", token_hash="e" * 64
    )
    assert not store.claim_event_token(
        identity, provider_run_id="sole-build", token_hash="f" * 64
    )
    with pytest.raises(ValueError):
        store.update_quality_bootstrap(
            identity,
            attempt_nonce=current["bootstrap_nonce"],
            changes={
                "build_id": "other-build",
                "provider_run_id": "other-build",
                "bootstrap_build_verified": True,
                "bootstrap_verified_request_hash": current[
                    "bootstrap_build_request_hash"
                ],
            },
        )


def test_bootstrap_and_github_admission_are_mutually_exclusive(backend):
    value = execution()

    def bootstrap():
        try:
            reserve(value)
            return "bootstrap"
        except ValueError:
            return "blocked"

    def github():
        try:
            store.admit_github_run(value["execution_id"], provider_run_id="42")
            return "github"
        except ValueError:
            return "blocked"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(function) for function in (bootstrap, github)]
        outcomes = [result.result() for result in results]
    assert outcomes.count("blocked") == 1
    current = store.get(value["execution_id"])
    if "bootstrap" in outcomes:
        assert current["provider"] == "cloud_build" and not current.get("github_run_id")
    else:
        assert (
            current["provider"] == "github_actions"
            and store.get_quality_bootstrap_ticket() is None
        )


def test_stale_callbacks_and_generic_provider_writes_cannot_overwrite_bootstrap(
    backend,
):
    value, _ = reserve(execution())
    identity = value["execution_id"]
    with pytest.raises(ValueError, match="provider changed"):
        store.save_for_provider(
            identity, expected_provider="github_actions", github_run_id=42
        )
    with pytest.raises(ValueError, match="provider changed"):
        store.admit_event_provider(
            identity,
            expected_provider="github_actions",
            expected_run_id="42",
            expected_token_hash="e" * 64,
        )
    with pytest.raises(ValueError):
        store.accept_event(
            identity,
            1,
            expected_provider="github_actions",
            expected_run_id="42",
            provider_run_id="42",
            status="running_quality",
        )
    assert store.get(identity)["event_sequence"] == 0


def test_old_failed_execution_is_preserved(backend):
    failed = execution("e" * 40)
    failed = store.save(
        failed["execution_id"], status="failed", error="prior quality failed"
    )
    reserve(execution())
    assert store.get(failed["execution_id"]) == failed


def test_returned_mutable_binding_cannot_modify_durable_ticket(backend):
    value, _ = reserve(execution())
    value["bootstrap_build_request"]["timeout"] = "7200s"
    value["bootstrap_binding"]["nonce"] = "changed"
    assert (
        store.get(value["execution_id"])["bootstrap_build_request"]["timeout"]
        == "3600s"
    )
    ticket = store.get_quality_bootstrap_ticket()
    ticket["build_request"]["timeout"] = "7200s"
    assert store.get_quality_bootstrap_ticket()["build_request"]["timeout"] == "3600s"


def test_persistent_transaction_failure_consumes_no_partial_ticket(backend):
    if backend is None:
        pytest.skip("Firestore transaction failure scenario")
    value = execution()
    backend.fail_commit = True
    with pytest.raises(RuntimeError, match="persistence unavailable"):
        reserve(value)
    assert store.get_quality_bootstrap_ticket() is None
    assert store.get(value["execution_id"])["status"] == "waiting_github"


def test_failed_post_outcome_write_keeps_global_ticket_consumed(backend):
    if backend is None:
        pytest.skip("Firestore transaction failure scenario")
    value, _ = reserve(execution())
    backend.fail_commit = True
    with pytest.raises(RuntimeError):
        store.update_quality_bootstrap(
            value["execution_id"],
            attempt_nonce=value["bootstrap_nonce"],
            changes={"status": "unknown"},
        )
    backend.fail_commit = False
    assert not reserve(value)[1]
    assert not store.claim_submission(value["execution_id"])


def test_production_never_falls_back_to_memory(monkeypatch):
    value, _ = reserve(execution())
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(store, "_collection", lambda: None)
    for operation in (
        store.get_quality_bootstrap_ticket,
        lambda: store.get(value["execution_id"]),
        lambda: reserve(value),
        lambda: store.save(value["execution_id"], status="unknown"),
        lambda: store.update_quality_bootstrap(
            value["execution_id"],
            attempt_nonce=value["bootstrap_nonce"],
            changes={"status": "unknown"},
        ),
    ):
        with pytest.raises(RuntimeError, match="Persistent"):
            operation()


def test_global_ticket_document_is_excluded_from_execution_and_scheduler_lists(backend):
    value, _ = reserve(execution())
    assert len(store.list_for_repository(value["repository"])) == 1
    assert len(store.list_due()) == 1
    assert (
        store.find(value["repository"], HEAD, "pr_quality")["execution_id"]
        == value["execution_id"]
    )


def test_resolve_stale_github_identity_cannot_admit_after_bootstrap(
    backend, monkeypatch
):
    value = execution()

    def verify(*_args):
        reserve(value)
        return {"run_id": "42"}

    monkeypatch.setattr(events.release_workflow_identity, "verify", verify)
    monkeypatch.setattr(
        events.github_release_control,
        "workflow_run",
        lambda *_: SimpleNamespace(
            event="pull_request_target",
            display_title=f"eng-platform-quality-{HEAD}",
            path=".github/workflows/eng-platform-quality.yml",
        ),
    )
    monkeypatch.setattr(events.catalog, "get_service", lambda _: object())
    monkeypatch.setattr(
        events.quality_profiles, "profile_for", lambda _: SimpleNamespace(spec={})
    )
    with pytest.raises(HTTPException) as error:
        events.resolve_execution(
            events.ResolveExecutionRequest(
                repository=value["repository"], head_sha=HEAD, operation="pr_quality"
            ),
            authorization="Bearer offline-identity",
        )
    assert error.value.status_code == 409
    current = store.get(value["execution_id"])
    assert current["provider"] == "cloud_build"
    assert not current.get("provider_run_id") and not current.get("github_run_id")


def test_late_workflow_webhook_does_not_overwrite_bootstrap(backend, monkeypatch):
    value = execution()

    def list_snapshot(*_args, **_kwargs):
        reserve(value)
        return [value]

    monkeypatch.setattr(store, "list_for_repository", list_snapshot)
    monkeypatch.setattr(orchestrator, "_close_circuit_from_probe", lambda *_: False)
    monkeypatch.setattr(orchestrator, "_handle_deployment_workflow_run", lambda *_: [])
    monkeypatch.setattr(
        orchestrator.github_release_control,
        "workflow_run",
        lambda *_: SimpleNamespace(event="pull_request_target"),
    )
    result = orchestrator.handle_workflow_run(
        {
            "action": "completed",
            "repository": {"full_name": value["repository"]},
            "workflow_run": {
                "id": 42,
                "event": "pull_request_target",
                "head_sha": BASE,
                "display_title": f"eng-platform-quality-{HEAD}",
                "path": ".github/workflows/eng-platform-quality.yml",
                "conclusion": "success",
            },
        },
        delivery_id="late-webhook",
    )
    assert result == []
    current = store.get(value["execution_id"])
    assert current["provider"] == "cloud_build"
    assert not current.get("github_run_id") and not current.get(
        "provider_terminal_success"
    )


def test_stale_event_is_rejected_before_pending_report_persistence(
    backend, monkeypatch
):
    from unittest.mock import Mock

    value = execution()
    monkeypatch.setattr(events, "_verify_provider", lambda *_: reserve(value))
    # Isolate the race after external identity verification; the transaction
    # must reject a changed provider before evidence storage is called.
    monkeypatch.setattr(events, "_verify_event_token", lambda *_: None)
    report = Mock()
    monkeypatch.setattr(events, "_verify_report", report)
    with pytest.raises(HTTPException) as error:
        events._accept_event(
            value["execution_id"],
            ReleaseExecutionEvent(
                execution_id=value["execution_id"],
                provider_run_id="42",
                fingerprint=value["fingerprint"],
                sequence=1,
                status="running_quality",
            ),
            "Bearer offline-identity",
            "offline-event-token",
        )
    assert error.value.status_code == 409
    report.assert_not_called()
    assert store.get(value["execution_id"])["event_sequence"] == 0


@pytest.mark.parametrize("token_endpoint", ["source", "event"])
def test_callback_cannot_bind_first_of_two_ambiguous_bootstrap_builds(
    backend, monkeypatch, token_endpoint
):
    from unittest.mock import Mock

    value, _ = reserve(execution())
    nonce = value["bootstrap_nonce"]
    candidates = [
        {"id": build_id, "substitutions": {"_BOOTSTRAP_NONCE": nonce}}
        for build_id in ("first-build", "second-build")
    ]
    session = SimpleNamespace(
        get=Mock(
            return_value=SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {"builds": candidates},
            )
        )
    )
    monkeypatch.setattr(
        events.release_cloud_build, "get_build", lambda _: candidates[0]
    )
    monkeypatch.setattr(events.release_cloud_build, "_session", lambda: session)
    # Two separately verified complete requests with the same one-shot nonce
    # still constitute ambiguity; identity verification alone cannot bind one.
    verified = Mock()
    monkeypatch.setattr(
        events.release_cloud_build, "verify_quality_bootstrap_build", verified
    )
    mint = Mock()
    monkeypatch.setattr(events.github_release_control, "installation_read_token", mint)
    endpoint = (
        events.issue_source_token
        if token_endpoint == "source"
        else events.issue_event_token
    )
    with pytest.raises(HTTPException) as error:
        endpoint(
            value["execution_id"],
            events.SourceTokenRequest(
                fingerprint=value["fingerprint"], provider_run_id="first-build"
            ),
            authorization="Bearer offline-identity",
        )
    assert error.value.status_code == 403
    assert verified.call_count == 2
    mint.assert_not_called()
    current = store.get(value["execution_id"])
    assert not current.get("build_id")
    assert not current.get("source_token_issued_at") and not current.get(
        "event_token_hash"
    )


def test_callback_rejects_altered_bound_bootstrap_build_before_token_claim(
    backend, monkeypatch
):
    value, _ = reserve(execution())
    value = verified_build(value)
    altered = {"id": "sole-build", "timeout": "7200s"}
    monkeypatch.setattr(events.release_cloud_build, "get_build", lambda _: altered)
    with pytest.raises(HTTPException) as error:
        events.issue_source_token(
            value["execution_id"],
            events.SourceTokenRequest(
                fingerprint=value["fingerprint"], provider_run_id="sole-build"
            ),
            authorization="Bearer offline-identity",
        )
    assert error.value.status_code == 403
    assert not store.get(value["execution_id"]).get("source_token_issued_at")


@pytest.mark.parametrize("terminal", ["failed", "quality_failed", "quality_passed"])
def test_terminal_bootstrap_cannot_emit_new_source_or_event_tokens(backend, terminal):
    value, _ = reserve(execution())
    value = verified_build(value)
    store.save(value["execution_id"], status=terminal)
    assert not store.claim_source_token(
        value["execution_id"], provider_run_id="sole-build"
    )
    assert not store.claim_event_token(
        value["execution_id"], provider_run_id="sole-build", token_hash="e" * 64
    )


def test_generic_writes_cannot_mutate_global_ticket_document(backend):
    reserve(execution())
    before = store.get_quality_bootstrap_ticket()
    with pytest.raises(ValueError, match="global ticket is immutable"):
        store.save(store._BOOTSTRAP_DOCUMENT_ID, idempotency_key="changed")
    with pytest.raises(ValueError, match="global ticket is immutable"):
        store.accept_event(store._BOOTSTRAP_DOCUMENT_ID, 1, status="waiting_github")
    assert store.get_quality_bootstrap_ticket() == before


def test_quality_passed_bootstrap_is_terminal_for_writes_and_event_admission(
    backend, monkeypatch
):
    from unittest.mock import Mock

    value, _ = reserve(execution())
    value = verified_build(value)
    token = "offline-token"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    assert store.claim_event_token(
        value["execution_id"], provider_run_id="sole-build", token_hash=token_hash
    )
    value = store.save(value["execution_id"], status="quality_passed")
    for status in ("unknown", "failed", "running_quality"):
        with pytest.raises(ValueError, match="cannot be reopened"):
            store.save(value["execution_id"], status=status)
        with pytest.raises(ValueError, match="cannot be reopened"):
            store.update_quality_bootstrap(
                value["execution_id"],
                attempt_nonce=value["bootstrap_nonce"],
                changes={"status": status},
            )
    monkeypatch.setattr(events, "_verify_provider", lambda *_: None)
    report = Mock()
    monkeypatch.setattr(events, "_verify_report", report)
    with pytest.raises(HTTPException) as error:
        events._accept_event(
            value["execution_id"],
            ReleaseExecutionEvent(
                execution_id=value["execution_id"],
                provider_run_id="sole-build",
                fingerprint=value["fingerprint"],
                sequence=2,
                status="running_quality",
            ),
            "Bearer offline-identity",
            token,
        )
    assert error.value.status_code == 409
    report.assert_not_called()
    assert store.get(value["execution_id"])["status"] == "quality_passed"


def test_verified_binding_rejects_incoherent_stored_provider_run(backend):
    value, _ = reserve(execution())
    value = verified_build(value)
    identity = value["execution_id"]
    # Model existing durable corruption without going through protected saves.
    # Verification must fail closed rather than silently repair another run.
    stored = store._memory[identity] if backend is None else backend.values[identity]
    stored["provider_run_id"] = "different-provider-run"
    before = store.get(identity)
    with pytest.raises(ValueError, match="binding is not verified"):
        store.update_quality_bootstrap(
            identity,
            attempt_nonce=value["bootstrap_nonce"],
            changes={
                "build_id": "sole-build",
                "provider_run_id": "sole-build",
                "bootstrap_build_verified": True,
                "bootstrap_verified_request_hash": value[
                    "bootstrap_build_request_hash"
                ],
            },
        )
    assert store.get(identity) == before
