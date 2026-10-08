"""Official probe control: no real workflows, credentials, or cloud writes."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import inspect
import json
from threading import RLock
from unittest.mock import Mock

from fastapi import HTTPException
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
import pytest

from eng_platform_api import mcp_server
from eng_platform_api.config import config
from eng_platform_api.routers import release_operations
from eng_platform_api.services import database_job_store, mcp_store
from eng_platform_api.services import executor_circuits as circuits
from eng_platform_api.services import release_orchestrator as orchestrator
from tests.mcp_helpers import Control, access_token
from tests.test_executor_circuits import _Collection

OWNER = "diegomad14"
REPOSITORY = "diegomad14/gcp-engineering-platform-api"
WORKFLOW = "eng-platform-actions-health.yml"


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setattr(config.mcp, "enabled", True)
    monkeypatch.setattr(config.mcp, "public_base_url", "http://testserver")
    monkeypatch.setattr(config.github, "billing_owner", OWNER)
    monkeypatch.setattr(
        config.release_orchestrator, "github_health_repository", REPOSITORY
    )
    monkeypatch.setattr(config.release_orchestrator, "github_health_workflow", WORKFLOW)
    monkeypatch.setattr(mcp_store, "_collection", lambda _: None)
    for values in mcp_store._memory.values():
        values.clear()
    with database_job_store.testing_backend(Control()):
        yield


@pytest.fixture(params=["memory", "firestore"])
def backend(request, monkeypatch):
    if request.param == "memory":
        yield None
        return
    from google.cloud import firestore

    collection = _Collection()
    lock = RLock()

    def transactional(function):
        def atomic(transaction):
            with lock:
                return function(transaction)

        return atomic

    monkeypatch.setattr(firestore, "transactional", transactional)
    monkeypatch.setattr(circuits, "_collection", lambda: collection)
    yield collection


@pytest.fixture
def dispatch(monkeypatch):
    value = Mock()
    monkeypatch.setattr(
        orchestrator.github_release_control, "dispatch_health_probe", value
    )
    return value


def open_circuit():
    circuits.open_circuit(OWNER, reason="github_actions_billing_rejection")


def request(key="key-one", reason="Billing repaired"):
    return orchestrator.request_health_probe(
        requested_by=OWNER, reason=reason, idempotency_key=key
    )


def complete(conclusion="success"):
    value = circuits.get(OWNER)
    return circuits.record_probe(
        OWNER,
        repository=REPOSITORY,
        workflow=WORKFLOW,
        run_id="42",
        conclusion=conclusion,
        jobs_started=1,
        nonce=value["probe"]["nonce"],
    )


@contextmanager
def authenticated(subject=OWNER, scopes=None):
    token = access_token(subject=subject, scopes=scopes)
    context = auth_context_var.set(AuthenticatedUser(token))
    try:
        yield token
    finally:
        auth_context_var.reset(context)


def test_duplicate_and_concurrent_requests_dispatch_once(backend, dispatch):
    open_circuit()
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: request(), range(24)))
    assert dispatch.call_count == 1
    assert all(value["status"] == "requested" for value in results)
    assert request() == request()
    with pytest.raises(ValueError, match="already pending"):
        request("different-key")
    with pytest.raises(ValueError, match="bound to another"):
        request(reason="Different request")
    with pytest.raises(ValueError, match="bound to another"):
        circuits.reserve_probe(
            OWNER,
            repository="another/repository",
            workflow=WORKFLOW,
            requested_by=OWNER,
            reason="Billing repaired",
            idempotency_key="key-one",
        )
    assert dispatch.call_count == 1


def test_uncertain_dispatch_never_retries_or_exposes_exception(backend, dispatch):
    open_circuit()
    dispatch.side_effect = RuntimeError("secret-token-provider-error")
    first = request()
    assert first["dispatch_status"] == "uncertain"
    assert request() == first
    with pytest.raises(ValueError, match="already pending"):
        request("fresh-key")
    assert dispatch.call_count == 1
    assert "secret" not in json.dumps(first)
    complete()
    circuits.close_after_successful_probe(OWNER, run_id="42")
    assert request()["state"] == "closed"
    assert dispatch.call_count == 1


def test_callback_then_reopen_keeps_all_keys_and_terminal_status(backend, dispatch):
    open_circuit()
    request()
    complete()
    circuits.close_after_successful_probe(OWNER, run_id="42")
    terminal = request()
    assert terminal["state"] == "closed" and terminal["status"] == "verified"
    open_circuit()
    assert request() == terminal
    request("second-key")
    complete("failure")
    request("third-key")
    assert request() == terminal
    assert request("second-key")["conclusion"] == "failure"
    assert dispatch.call_count == 3


def test_history_is_bounded_without_eviction(backend, dispatch, monkeypatch):
    monkeypatch.setattr(circuits, "_PROBE_REQUEST_LIMIT", 2)
    open_circuit()
    request()
    complete("failure")
    request("second-key")
    complete("failure")
    with pytest.raises(ValueError, match="history is full"):
        request("third-key")
    assert request()["status"] == "verified"
    assert dispatch.call_count == 2


def test_legacy_pending_probe_is_not_overwritten(backend, dispatch):
    open_circuit()
    state = circuits.get(OWNER)
    state["probe"] = {"status": "requested", "nonce": "legacy-secret"}
    if backend is None:
        circuits._memory[circuits._id(OWNER)] = state
    else:
        backend.document(circuits._id(OWNER)).value = state
    with pytest.raises(ValueError, match="already pending"):
        request()
    assert circuits.get(OWNER)["probe"]["nonce"] == "legacy-secret"
    dispatch.assert_not_called()


def test_dispatch_callback_race_keeps_verified_result(backend, dispatch):
    open_circuit()

    def callback(*args, **kwargs):
        complete()
        circuits.close_after_successful_probe(OWNER, run_id="42")

    dispatch.side_effect = callback
    result = request()
    assert result["state"] == "closed"
    assert result["status"] == "verified"
    assert result["dispatch_status"] == "dispatched"
    assert request() == result
    assert dispatch.call_count == 1


def test_firestore_transaction_reads_and_reserves_inside_transaction(
    dispatch, monkeypatch
):
    from google.cloud import firestore

    collection = _Collection()
    document = collection.document(circuits._id(OWNER))
    document.value = {"state": "open"}
    get = Mock(wraps=document.get)
    document.get = get
    monkeypatch.setattr(circuits, "_collection", lambda: collection)
    monkeypatch.setattr(firestore, "transactional", lambda function: function)
    request()
    assert get.call_args_list and all(
        call.kwargs.get("transaction") for call in get.call_args_list
    )
    assert document.value["probe_requests"]
    assert dispatch.call_count == 1


@pytest.mark.parametrize("commit_before_error", [False, True])
def test_firestore_reservation_failure_never_dispatches(
    dispatch, monkeypatch, commit_before_error
):
    from google.cloud import firestore

    collection = _Collection()
    document = collection.document(circuits._id(OWNER))
    document.value = {"state": "open"}
    monkeypatch.setattr(circuits, "_collection", lambda: collection)

    def transactional(function):
        def fail(transaction):
            if commit_before_error:
                function(transaction)
            raise RuntimeError("credential-secret")

        return fail

    monkeypatch.setattr(firestore, "transactional", transactional)
    with pytest.raises(
        orchestrator.ReleaseOrchestratorError, match="reservation is unavailable"
    ):
        request()
    dispatch.assert_not_called()
    if commit_before_error:
        monkeypatch.setattr(firestore, "transactional", lambda function: function)
        assert request()["dispatch_status"] == "reserved"
        dispatch.assert_not_called()


def test_post_dispatch_storage_failure_keeps_fence(backend, dispatch, monkeypatch):
    open_circuit()
    original = circuits.finish_probe_dispatch
    monkeypatch.setattr(
        circuits, "finish_probe_dispatch", Mock(side_effect=RuntimeError("private"))
    )
    with pytest.raises(
        orchestrator.ReleaseOrchestratorError, match="do not dispatch another"
    ):
        request()
    monkeypatch.setattr(circuits, "finish_probe_dispatch", original)
    assert request()["dispatch_status"] == "reserved"
    with pytest.raises(ValueError, match="already pending"):
        request("fresh-key")
    assert dispatch.call_count == 1


def test_production_missing_persistence_fails_closed(dispatch, monkeypatch):
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(circuits, "_collection", lambda: None)
    with pytest.raises(
        orchestrator.ReleaseOrchestratorError, match="reservation is unavailable"
    ):
        request()
    assert circuits._memory == {}
    dispatch.assert_not_called()


def test_mcp_public_schema_auth_scope_and_sanitized_audit(dispatch):
    assert list(
        inspect.signature(mcp_server.request_github_actions_health_probe).parameters
    ) == ["reason", "idempotency_key"]
    open_circuit()
    with pytest.raises(HTTPException) as error:
        mcp_server.request_github_actions_health_probe("reason", "key")
    assert error.value.status_code == 401
    with authenticated(scopes=["eng-platform.read"]):
        with pytest.raises(HTTPException) as error:
            mcp_server.request_github_actions_health_probe("reason", "key")
        assert error.value.status_code == 403
    with authenticated():
        result = mcp_server.request_github_actions_health_probe(
            "private-reason", "private-key"
        )
    assert result["dispatch_status"] == "dispatched"
    serialized = json.dumps(result)
    for forbidden in (
        "nonce",
        "request_key",
        "probe_requests",
        "token",
        "private-reason",
        "private-key",
    ):
        assert forbidden not in serialized
    audits = list(mcp_store._memory["audit"].values())
    assert audits[-1]["tool"] == "request_github_actions_health_probe"
    assert audits[-1]["result"] == "accepted" and audits[-1]["mutation"]
    assert "private-reason" not in json.dumps(audits)
    assert "private-key" not in json.dumps(audits)


@pytest.mark.parametrize("allowed", [(), ("somebody-else",)])
def test_mcp_deployer_allowlist_is_required(dispatch, monkeypatch, allowed):
    open_circuit()
    monkeypatch.setattr(config.auth, "allowed_logins", allowed)
    with authenticated():
        with pytest.raises(HTTPException) as error:
            mcp_server.request_github_actions_health_probe("reason", "key")
    assert error.value.status_code == 403
    dispatch.assert_not_called()
    assert not circuits.get(OWNER).get("probe")
    assert list(mcp_store._memory["audit"].values())[-1]["result"] == "error"


@pytest.mark.parametrize("when", ["before", "after_reservation"])
def test_mcp_revocation_is_rechecked_before_dispatch(dispatch, monkeypatch, when):
    open_circuit()
    with authenticated() as token:
        record = mcp_server.provider._credential("access", token.token)

        def revoke():
            database_job_store.mutate(
                [("session", record["session_id"])],
                lambda values: {
                    key: {**value, "revoked_at": 1} for key, value in values.items()
                },
            )

        if when == "before":
            revoke()
        else:
            original = circuits.reserve_probe

            def reserve(*args, **kwargs):
                result = original(*args, **kwargs)
                revoke()
                return result

            monkeypatch.setattr(circuits, "reserve_probe", reserve)
        with pytest.raises(HTTPException) as error:
            mcp_server.request_github_actions_health_probe("reason", "key")
        assert error.value.status_code == 401
    dispatch.assert_not_called()
    if when == "after_reservation":
        assert circuits.get(OWNER)["probe"]["status"] == "cancelled"


def test_mcp_rate_limit_and_conflicts_are_audited(dispatch, monkeypatch):
    open_circuit()
    with authenticated():
        mcp_server.request_github_actions_health_probe("reason", "key")
        with pytest.raises(HTTPException) as error:
            mcp_server.request_github_actions_health_probe("different", "key")
        assert error.value.status_code == 409
        monkeypatch.setattr(config.mcp, "mutation_limit_per_hour", 2)
        with pytest.raises(HTTPException) as error:
            mcp_server.request_github_actions_health_probe("reason", "key")
        assert error.value.status_code == 429
    assert dispatch.call_count == 1
    assert any(
        item["result"] == "error" for item in mcp_store._memory["audit"].values()
    )


@pytest.mark.parametrize(
    "reason,key",
    [
        ("", "key"),
        (" ", "key"),
        ("x" * 501, "key"),
        ("reason", ""),
        ("reason", "x" * 129),
    ],
)
def test_invalid_inputs_fail_without_dispatch(dispatch, reason, key):
    with pytest.raises(HTTPException) as error:
        mcp_server.request_github_actions_health_probe(reason, key)
    assert error.value.status_code == 422
    dispatch.assert_not_called()


def test_web_endpoint_stays_body_optional_and_sanitized(dispatch):
    open_circuit()
    value = release_operations.request_probe(identity=OWNER)
    assert value["accepted"] and value["repository"] == REPOSITORY
    assert value["state"] == "open"
    assert "nonce" not in json.dumps(value)
    with pytest.raises(HTTPException) as error:
        release_operations.request_probe(identity=OWNER)
    assert error.value.status_code == 409
    assert dispatch.call_count == 1


def test_callback_cannot_record_wrong_nonce(backend, dispatch):
    open_circuit()
    request()
    before = deepcopy(circuits.get(OWNER))
    with pytest.raises(ValueError, match="does not match"):
        circuits.record_probe(
            OWNER,
            repository=REPOSITORY,
            workflow=WORKFLOW,
            run_id="42",
            conclusion="success",
            jobs_started=1,
            nonce="wrong-nonce",
        )
    assert circuits.get(OWNER) == before


def test_revocation_cancellation_store_failure_is_sanitized(dispatch, monkeypatch):
    open_circuit()
    with authenticated() as token:
        record = mcp_server.provider._credential("access", token.token)
        original = circuits.reserve_probe

        def reserve(*args, **kwargs):
            result = original(*args, **kwargs)
            database_job_store.mutate(
                [("session", record["session_id"])],
                lambda values: {
                    key: {**value, "revoked_at": 1} for key, value in values.items()
                },
            )
            return result

        monkeypatch.setattr(circuits, "reserve_probe", reserve)
        monkeypatch.setattr(
            circuits,
            "finish_probe_dispatch",
            Mock(side_effect=RuntimeError("secret-sentinel")),
        )
        with pytest.raises(HTTPException) as error:
            mcp_server.request_github_actions_health_probe("reason", "key")
        assert error.value.status_code == 503
        assert "secret-sentinel" not in str(error.value)
    dispatch.assert_not_called()
    assert circuits.get(OWNER)["probe"]["status"] == "requested"


def test_concurrent_different_keys_have_one_reservation_winner(backend, dispatch):
    open_circuit()

    def attempt(index):
        try:
            return request(f"key-{index}")
        except ValueError as exc:
            assert "already pending" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(attempt, range(24)))
    assert sum(value is not None for value in results) == 1
    assert dispatch.call_count == 1


@pytest.mark.parametrize("state_after_retry", ["open", "closed"])
def test_firestore_retry_rechecks_state_and_dispatches_only_after_commit(
    dispatch, monkeypatch, state_after_retry
):
    from google.cloud import firestore

    collection = _Collection()
    document = collection.document(circuits._id(OWNER))
    document.value = {"state": "open"}
    monkeypatch.setattr(circuits, "_collection", lambda: collection)
    calls = []

    def transactional(function):
        def retried(transaction):
            snapshot = deepcopy(document.value)
            result = function(transaction)
            # Simulate an aborted first attempt: none of its writes committed.
            if not calls:
                document.value = snapshot
                document.value["state"] = state_after_retry
                dispatch.assert_not_called()
                calls.append("retry")
                result = function(transaction)
            return result

        return retried

    monkeypatch.setattr(firestore, "transactional", transactional)
    if state_after_retry == "closed":
        with pytest.raises(ValueError, match="circuit is not open"):
            request()
        dispatch.assert_not_called()
        assert "probe" not in document.value
    else:
        assert request()["dispatch_status"] == "dispatched"
        assert dispatch.call_count == 1
        assert len(document.value["probe_requests"]) == 1
    assert calls == ["retry"]


def test_provider_value_error_is_not_treated_as_public_conflict(dispatch, monkeypatch):
    monkeypatch.setattr(
        circuits, "_collection", Mock(side_effect=ValueError("secret-sentinel"))
    )
    with authenticated():
        with pytest.raises(HTTPException) as error:
            mcp_server.request_github_actions_health_probe("reason", "key")
        assert error.value.status_code == 503
        assert "secret-sentinel" not in str(error.value)
    dispatch.assert_not_called()


def test_service_authority_store_failure_is_sanitized(dispatch, monkeypatch):
    with authenticated():
        monkeypatch.setattr(
            orchestrator.mcp_grants,
            "release_claims",
            Mock(side_effect=RuntimeError("authority-secret-sentinel")),
        )
        with pytest.raises(HTTPException) as error:
            mcp_server.request_github_actions_health_probe("reason", "key")
        assert error.value.status_code == 503
        assert "secret-sentinel" not in str(error.value)
    dispatch.assert_not_called()
