"""The observation route cannot alter canonical quality or broaden run authority."""

import asyncio
import copy
import json
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from eng_platform_api.routers import release_execution_events as events
from eng_platform_api.services import quality_store
from tests.test_quality_evidence import execution, observation, receipt


def request(chunks):
    chunks = iter(chunks)

    async def receive():
        value = next(chunks, None)
        if value is None:
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.request", "body": value, "more_body": True}

    return Request(
        {"type": "http", "method": "POST", "path": "/", "headers": []}, receive
    )


def payload():
    return events.ObservationRequest(
        fingerprint="3" * 64,
        provider_run_id="123",
        report_hash="7" * 64,
        observation=observation(),
    )


@pytest.fixture
def auth(monkeypatch):
    source = execution()
    monkeypatch.setattr(events.release_executions, "get", lambda _: source)
    provider = Mock()
    token = Mock()
    monkeypatch.setattr(events, "_verify_provider", provider)
    monkeypatch.setattr(events, "_verify_event_token", token)
    save = Mock(return_value=receipt())
    monkeypatch.setattr(events.quality_observation_store, "save_observation", save)
    lifecycle_save = Mock(side_effect=AssertionError("Observation changed lifecycle"))
    reconcile = Mock(side_effect=AssertionError("Observation invoked planning"))
    monkeypatch.setattr(events.release_executions, "save", lifecycle_save)
    monkeypatch.setattr(events.release_reconciler, "reconcile", reconcile)
    return source, provider, token, save


def test_accepted_observation_reuses_exact_auth_and_server_context(auth):
    source, provider, token, save = auth
    before = copy.deepcopy(source)
    result = events._accept_test_observation(
        "fake-execution", payload(), "Bearer fake", "fake-event-token"
    )
    provider.assert_called_once_with(source, "123", "Bearer fake")
    token.assert_called_once_with(source, "fake-event-token")
    save.assert_called_once_with(observation(), source, "7" * 64)
    assert result["accepted"] is True
    assert result["reuse_allowed"] is False
    assert source == before


@pytest.mark.parametrize(
    "failure", ["unknown", "fingerprint", "provider", "token", "run"]
)
def test_rejected_authority_never_writes(auth, monkeypatch, failure):
    source, provider, token, save = auth
    if failure == "unknown":
        monkeypatch.setattr(events.release_executions, "get", lambda _: None)
    elif failure == "fingerprint":
        source["fingerprint"] = "8" * 64
    elif failure == "provider":
        provider.side_effect = HTTPException(status_code=403)
    elif failure == "token":
        token.side_effect = HTTPException(status_code=403)
    elif failure == "run":
        source["provider_run_id"] = "other"
    with pytest.raises(HTTPException):
        events._accept_test_observation(
            "fake-execution", payload(), "Bearer fake", "fake-event-token"
        )
    save.assert_not_called()


def test_valid_stream_accepts_and_replay_conflict_is_independent(auth):
    body = payload().model_dump_json().encode()
    result = asyncio.run(
        events.record_test_observations(
            "fake-execution",
            request([body[:20], body[20:]]),
            "Bearer fake",
            "fake-event-token",
        )
    )
    assert result["reuse_allowed"] is False
    auth[3].side_effect = quality_store.QualityEvidenceConflict("private text")
    with pytest.raises(HTTPException) as caught:
        events._accept_test_observation(
            "fake-execution", payload(), "Bearer fake", "fake-event-token"
        )
    assert caught.value.status_code == 409
    assert "private text" not in caught.value.detail


@pytest.mark.parametrize(
    "body,status",
    [(b"x" * 1_000_001, 413), (b'{"bad":"private-input"}', 422), (b'{"invalid":', 422)],
)
def test_size_limit_before_parse_and_sanitized_errors(auth, body, status):
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            events.record_test_observations(
                "fake-execution",
                request([body[:500_000], body[500_000:]]),
                "Bearer fake",
                "fake-event-token",
            )
        )
    assert caught.value.status_code == status
    assert "private-input" not in caught.value.detail
    auth[1].assert_not_called()
    auth[3].assert_not_called()


def test_envelope_cannot_submit_provenance_or_unknown_fields(auth):
    value = payload().model_dump()
    value["execution"] = {"repository": "another/private-repository"}
    with pytest.raises(HTTPException) as caught:
        asyncio.run(
            events.record_test_observations(
                "fake-execution",
                request([json.dumps(value).encode()]),
                "Bearer fake",
                "fake-event-token",
            )
        )
    assert caught.value.status_code == 422
    auth[3].assert_not_called()


def test_pending_callback_report_is_enough_before_provider_terminal(
    monkeypatch, tmp_path
):
    from eng_platform_api.models import QualityReportCreate
    from eng_platform_api.services import quality_observation_store

    monkeypatch.setenv("ENG_PLATFORM_QUALITY_BUCKET", "")
    monkeypatch.setenv("ENG_PLATFORM_QUALITY_STORE_PATH", str(tmp_path))
    monkeypatch.setattr(quality_store, "require_managed_service_name", lambda _: None)
    monkeypatch.setattr(
        quality_observation_store, "require_managed_service_name", lambda _: None
    )
    server = execution()
    server.update(status="running_quality", event_sequence=1)
    server.pop("pending_report_hash")
    server.pop("engine_event_status")
    report = QualityReportCreate(
        service_name="example-api",
        repository="example/test-repo",
        commit_sha="1" * 40,
        base_sha="2" * 40,
        generated_at="2026-01-01T00:00:00+00:00",
        profile="python",
        policy_version="oss-v2",
        coverage=90,
        checks=[{"name": "Tests", "category": "tests", "status": "PASSED"}],
    )
    accepted_hash = quality_store._report_hash(report)
    callback = events.ReleaseExecutionEvent(
        execution_id="fake-execution",
        provider_run_id="123",
        fingerprint="3" * 64,
        sequence=2,
        status="quality_passed",
        report=report,
        report_hash=accepted_hash,
    )
    monkeypatch.setattr(events.release_executions, "get", lambda _: server)
    monkeypatch.setattr(events, "_verify_provider", lambda *_: None)
    monkeypatch.setattr(events, "_verify_event_token", lambda *_: None)

    def accept_event(execution_id, sequence, **changes):
        server.update(changes, event_sequence=sequence)
        return server, True

    monkeypatch.setattr(events.release_executions, "accept_event", accept_event)
    # The provider remains running: reconciliation must not invent final evidence.
    monkeypatch.setattr(events.release_reconciler, "reconcile", lambda _: server)
    response = events._accept_event(
        "fake-execution", callback, "Bearer fake", "fake-event-token"
    )
    assert response["accepted"] is True
    assert response["status"] == "running_quality"
    assert quality_store.get_report("example-api", "1" * 40) is None
    assert quality_store.get_pending_report("fake-execution", accepted_hash) is not None
    before = copy.deepcopy(server)
    observations = payload().model_copy(update={"report_hash": accepted_hash})
    result = events._accept_test_observation(
        "fake-execution", observations, "Bearer fake", "fake-event-token"
    )
    assert result["accepted"] is True
    assert result["reuse_allowed"] is False
    assert server == before
    assert quality_store.get_report("example-api", "1" * 40) is None
    assert (
        quality_observation_store.get_observation("3" * 64)["report_sha256"]
        == accepted_hash
    )


@pytest.mark.parametrize(
    "has_build,valid_token", [(False, False), (False, True), (True, False)]
)
def test_observation_cannot_bind_or_mutate_lifecycle_via_provider_verifier(
    monkeypatch, has_build, valid_token
):
    import hashlib

    server = {
        **execution(),
        "provider": "cloud_build",
        "provider_run_id": "fake-build",
        "build_id": "fake-build" if has_build else "",
        "status": "running_quality",
        "event_token_hash": hashlib.sha256(b"fake-event-token").hexdigest(),
    }
    before = copy.deepcopy(server)
    monkeypatch.setattr(events.release_executions, "get", lambda _: server)
    bind = Mock(side_effect=AssertionError("Observation attempted build binding"))
    get_build = Mock(
        side_effect=AssertionError("Rejected observation queried provider")
    )
    monkeypatch.setattr(events.release_cloud_build, "_bind", bind)
    monkeypatch.setattr(events.release_cloud_build, "get_build", get_build)
    save = Mock(side_effect=AssertionError("Rejected observation wrote data"))
    monkeypatch.setattr(events.quality_observation_store, "save_observation", save)
    # Keep the real _verify_provider and event-token verifier in place.
    data = payload().model_copy(update={"provider_run_id": "fake-build"})
    with pytest.raises(HTTPException):
        events._accept_test_observation(
            "fake-execution",
            data,
            "Bearer fake",
            "fake-event-token" if valid_token else "invalid",
        )
    assert server == before
    bind.assert_not_called()
    get_build.assert_not_called()
    save.assert_not_called()


def test_unaccepted_callback_rejected_before_provider_verification(auth):
    source, provider, token, save = auth
    source.pop("pending_report_hash")
    with pytest.raises(HTTPException) as caught:
        events._accept_test_observation(
            "fake-execution", payload(), "Bearer fake", "fake-event-token"
        )
    assert caught.value.status_code == 422
    provider.assert_not_called()
    save.assert_not_called()
