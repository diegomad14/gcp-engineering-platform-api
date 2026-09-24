"""Security and ordering tests for private release-engine callbacks."""

from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from eng_platform_api.models import (
    QualityCheck,
    QualityReportCreate,
    ReleaseExecutionEvent,
    ReleasePlan,
)
from eng_platform_api.routers import release_execution_events as events
from eng_platform_api.services import release_workflow_identity


HEAD = "a" * 40
BASE = "b" * 40
FINGERPRINT = "f" * 64
PLANNER_HASH = "c" * 64
EXECUTOR_DIGEST = "quality@sha256:" + "d" * 64
SERVICE_ACCOUNT = "quality@test-project.iam.gserviceaccount.com"
EVENT_TOKEN = "event-token"
REPOSITORY_RESOURCE = (
    "projects/test-project/locations/us-central1/"
    "connections/github/repositories/eng-platform-web"
)


@pytest.fixture(autouse=True)
def call_async_event_endpoint_from_sync_tests(monkeypatch):
    monkeypatch.setattr(
        events.config.cloud_build,
        "repositories",
        {"eng-platform-web": REPOSITORY_RESOURCE},
    )
    endpoint = events.accept_event

    def invoke(*args, **kwargs):
        kwargs.setdefault("x_eng_platform_event_token", EVENT_TOKEN)
        return asyncio.run(endpoint(*args, **kwargs))

    monkeypatch.setattr(events, "accept_event", invoke)


def _execution(**overrides) -> dict:
    value = {
        "execution_id": "execution-1",
        "fingerprint": FINGERPRINT,
        "repository": "owner/eng-platform-web",
        "service_name": "eng-platform-web",
        "operation": "pr_quality",
        "head_sha": HEAD,
        "base_sha": BASE,
        "profile_hash": "profile-hash",
        "executor_digest": EXECUTOR_DIGEST,
        "planner_hash": "",
        "provider": "cloud_build",
        "build_id": "build-1",
        "status": "running_quality",
        "event_sequence": 0,
        "event_token_hash": hashlib.sha256(EVENT_TOKEN.encode()).hexdigest(),
    }
    value.update(overrides)
    return value


def _report(**overrides) -> QualityReportCreate:
    value = {
        "service_name": "eng-platform-web",
        "repository": "owner/eng-platform-web",
        "commit_sha": HEAD,
        "base_sha": BASE,
        "branch": "feature/test",
        "profile": "node",
        "generated_at": "2026-09-22T12:00:00+00:00",
        "coverage": 90.0,
        "coverage_threshold": 80.0,
        "policy_version": "oss-v2",
        "differential_coverage": 90.0,
        "differential_threshold": 80.0,
        "changed_lines": 10,
        "covered_changed_lines": 9,
        "checks": [
            QualityCheck(
                name="unit tests",
                category="tests",
                status="PASSED",
            )
        ],
    }
    value.update(overrides)
    return QualityReportCreate(**value)


def _event(**overrides) -> ReleaseExecutionEvent:
    value = {
        "execution_id": "execution-1",
        "provider_run_id": "build-1",
        "fingerprint": FINGERPRINT,
        "sequence": 1,
        "status": "quality_passed",
    }
    value.update(overrides)
    return ReleaseExecutionEvent(**value)


def _request(content_length: str = "100", body: bytes = b"{}") -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"content-length", content_length.encode())],
        },
        receive,
    )


def _build(execution: dict | None = None, **overrides) -> dict:
    execution = execution or _execution()
    value = {
        "id": execution["build_id"],
        "status": "SUCCESS",
        "serviceAccount": SERVICE_ACCOUNT,
        "source": {
            "connectedRepository": {
                "repository": REPOSITORY_RESOURCE,
                "revision": execution["head_sha"],
            }
        },
        "substitutions": {
            "_EXECUTION_ID": execution["execution_id"],
            "_REQUEST_FINGERPRINT": execution["fingerprint"],
            "_SERVICE_NAME": execution["service_name"],
            "_REPOSITORY": execution["repository"],
            "_HEAD_SHA": execution["head_sha"],
            "_BASE_SHA": execution["base_sha"],
            "_EXECUTOR_DIGEST": execution["executor_digest"],
            "_OPERATION": execution["operation"],
            "_PROFILE_SHA256": execution["profile_hash"],
        },
    }
    value.update(overrides)
    return value


def _pr_workflow_run(execution: dict | None = None, **overrides):
    execution = execution or _execution(provider="github_actions")
    value = {
        "head_sha": execution["head_sha"],
        "event": "pull_request_target",
        "display_title": f"eng-platform-quality-{execution['head_sha']}",
        "path": ".github/workflows/eng-platform-quality.yml",
    }
    value.update(overrides)
    return SimpleNamespace(**value)


@pytest.mark.parametrize("authorization", [None, "", "Basic token", "Bearer", "token"])
def test_bearer_requires_nonempty_bearer_token(authorization):
    with pytest.raises(HTTPException) as error:
        events._bearer(authorization)
    assert error.value.status_code == 401


def test_bearer_returns_token_without_logging_or_transforming_it():
    assert events._bearer("bearer secret-token") == "secret-token"


def test_verify_google_returns_test_identity_only_in_mock_mode(monkeypatch):
    monkeypatch.setattr(events.config, "mock_mode", True)
    monkeypatch.setattr(
        events.config.release_orchestrator,
        "callback_service_account",
        SERVICE_ACCOUNT,
    )
    assert events._verify_google("ignored") == {
        "email": SERVICE_ACCOUNT,
        "sub": "test",
    }


def test_verify_google_requires_configured_callback_identity(monkeypatch):
    monkeypatch.setattr(events.config, "mock_mode", False)
    monkeypatch.setattr(
        events.config.release_orchestrator, "callback_service_account", ""
    )
    with pytest.raises(HTTPException) as error:
        events._verify_google("token")
    assert error.value.status_code == 503


def test_verify_google_accepts_exact_oidc_email(monkeypatch):
    from google.oauth2 import id_token

    monkeypatch.setattr(events.config, "mock_mode", False)
    monkeypatch.setattr(
        events.config.release_orchestrator,
        "callback_service_account",
        SERVICE_ACCOUNT,
    )
    monkeypatch.setattr(events.config.github, "platform_api_url", "https://platform")
    verify = mock.Mock(return_value={"email": SERVICE_ACCOUNT, "sub": "subject"})
    monkeypatch.setattr(id_token, "verify_oauth2_token", verify)

    claims = events._verify_google("token")

    assert claims["sub"] == "subject"
    assert verify.call_args.args[0] == "token"
    assert verify.call_args.args[2] == "https://platform"


@pytest.mark.parametrize(
    "claims_or_error",
    [{"email": "other@example.com"}, ValueError("invalid token")],
)
def test_verify_google_rejects_invalid_or_wrong_service_account(
    monkeypatch, claims_or_error
):
    from google.oauth2 import id_token

    monkeypatch.setattr(events.config, "mock_mode", False)
    monkeypatch.setattr(
        events.config.release_orchestrator,
        "callback_service_account",
        SERVICE_ACCOUNT,
    )
    verify = mock.Mock()
    if isinstance(claims_or_error, Exception):
        verify.side_effect = claims_or_error
    else:
        verify.return_value = claims_or_error
    monkeypatch.setattr(id_token, "verify_oauth2_token", verify)

    with pytest.raises(HTTPException) as error:
        events._verify_google("secret-token")
    assert error.value.status_code == 401
    assert "secret-token" not in error.value.detail


def test_verify_cloud_build_accepts_exact_identity(monkeypatch):
    execution = _execution()
    monkeypatch.setattr(
        events.config.release_orchestrator, "service_account", SERVICE_ACCOUNT
    )
    monkeypatch.setattr(events.release_cloud_build, "get_build", lambda _: _build())

    assert events._verify_cloud_build(execution, "build-1")["status"] == "SUCCESS"


def test_verify_cloud_build_requires_bound_build_id(monkeypatch):
    get_build = mock.Mock()
    monkeypatch.setattr(events.release_cloud_build, "get_build", get_build)

    with pytest.raises(HTTPException) as error:
        events._verify_cloud_build(_execution(), "other-build")

    assert error.value.status_code == 403
    get_build.assert_not_called()


@pytest.mark.parametrize(
    "source",
    [
        {},
        {
            "connectedRepository": {
                "repository": "projects/other/locations/us-central1/connections/x/repositories/y",
                "revision": HEAD,
            }
        },
        {
            "connectedRepository": {
                "repository": REPOSITORY_RESOURCE,
                "revision": "0" * 40,
            }
        },
    ],
)
def test_verify_cloud_build_rejects_connected_repository_source_mismatch(
    monkeypatch, source
):
    monkeypatch.setattr(
        events.release_cloud_build,
        "get_build",
        lambda _: _build(source=source),
    )

    with pytest.raises(HTTPException) as error:
        events._verify_cloud_build(_execution(), "build-1")

    assert error.value.status_code == 403
    assert error.value.detail == "Cloud Build source mismatch"


def test_verify_cloud_build_hides_provider_lookup_error(monkeypatch):
    monkeypatch.setattr(
        events.release_cloud_build,
        "get_build",
        mock.Mock(side_effect=RuntimeError("provider response with secret")),
    )
    with pytest.raises(HTTPException) as error:
        events._verify_cloud_build(_execution(), "build-1")
    assert error.value.status_code == 502
    assert "secret" not in error.value.detail


@pytest.mark.parametrize(
    "key",
    [
        "_EXECUTION_ID",
        "_REQUEST_FINGERPRINT",
        "_SERVICE_NAME",
        "_REPOSITORY",
        "_HEAD_SHA",
        "_BASE_SHA",
        "_EXECUTOR_DIGEST",
        "_OPERATION",
        "_PROFILE_SHA256",
    ],
)
@pytest.mark.parametrize("mode", ["missing", "mismatch"])
def test_verify_cloud_build_requires_every_exact_substitution(monkeypatch, key, mode):
    build = _build()
    if mode == "missing":
        del build["substitutions"][key]
    else:
        build["substitutions"][key] = "attacker-controlled"
    monkeypatch.setattr(events.release_cloud_build, "get_build", lambda _: build)
    monkeypatch.setattr(
        events.config.release_orchestrator, "service_account", SERVICE_ACCOUNT
    )

    with pytest.raises(HTTPException) as error:
        events._verify_cloud_build(_execution(), "build-1")
    assert error.value.status_code == 403
    assert error.value.detail == "Cloud Build source mismatch"


@pytest.mark.parametrize("account", ["", "other@test-project.iam.gserviceaccount.com"])
def test_verify_cloud_build_requires_exact_nonempty_service_account(
    monkeypatch, account
):
    build = _build(serviceAccount=account)
    monkeypatch.setattr(events.release_cloud_build, "get_build", lambda _: build)
    monkeypatch.setattr(
        events.config.release_orchestrator, "service_account", SERVICE_ACCOUNT
    )

    with pytest.raises(HTTPException) as error:
        events._verify_cloud_build(_execution(), "build-1")
    assert error.value.status_code == 403
    assert error.value.detail == "Cloud Build service account mismatch"


def test_verify_provider_checks_google_and_build_for_cloud_build(monkeypatch):
    google = mock.Mock()
    build = mock.Mock()
    monkeypatch.setattr(events, "_verify_google", google)
    monkeypatch.setattr(events, "_verify_cloud_build", build)

    events._verify_provider(_execution(), "build-1", "Bearer token")

    google.assert_called_once_with("token")
    build.assert_called_once_with(_execution(), "build-1")


def test_verify_provider_maps_workflow_identity_error_to_unauthorized(monkeypatch):
    monkeypatch.setattr(
        events.release_workflow_identity,
        "verify",
        mock.Mock(
            side_effect=release_workflow_identity.ReleaseWorkflowIdentityError(
                "Untrusted quality workflow"
            )
        ),
    )
    with pytest.raises(HTTPException) as error:
        events._verify_provider(
            _execution(provider="github_actions"), "42", "Bearer token"
        )
    assert error.value.status_code == 401


def test_verify_provider_requires_claimed_run_id(monkeypatch):
    monkeypatch.setattr(
        events.release_workflow_identity,
        "verify",
        lambda *_: {"run_id": "41"},
    )
    with pytest.raises(HTTPException) as error:
        events._verify_provider(
            _execution(provider="github_actions"), "42", "Bearer token"
        )
    assert error.value.status_code == 403


def test_verify_provider_checks_pr_run_identity(monkeypatch):
    execution = _execution(provider="github_actions")
    monkeypatch.setattr(
        events.release_workflow_identity,
        "verify",
        lambda *_: {"run_id": "42"},
    )
    workflow_run = mock.Mock(return_value=_pr_workflow_run(execution))
    monkeypatch.setattr(events.github_release_control, "workflow_run", workflow_run)

    events._verify_provider(execution, "42", "Bearer token")

    workflow_run.assert_called_once_with(execution["repository"], 42)


def test_verify_provider_rejects_unreadable_or_wrong_pr_run(monkeypatch):
    execution = _execution(provider="github_actions")
    monkeypatch.setattr(
        events.release_workflow_identity,
        "verify",
        lambda *_: {"run_id": "42"},
    )
    monkeypatch.setattr(
        events.github_release_control,
        "workflow_run",
        mock.Mock(side_effect=RuntimeError("unavailable")),
    )
    with pytest.raises(HTTPException) as error:
        events._verify_provider(execution, "42", "Bearer token")
    assert error.value.status_code == 502

    monkeypatch.setattr(
        events.github_release_control,
        "workflow_run",
        lambda *_: _pr_workflow_run(execution, display_title="untrusted-run"),
    )
    with pytest.raises(HTTPException) as error:
        events._verify_provider(execution, "42", "Bearer token")
    assert error.value.status_code == 403


def test_verify_provider_does_not_lookup_run_again_for_main(monkeypatch):
    execution = _execution(operation="main_release", provider="github_actions")
    monkeypatch.setattr(
        events.release_workflow_identity,
        "verify",
        lambda *_: {"run_id": "42"},
    )
    workflow_run = mock.Mock()
    monkeypatch.setattr(events.github_release_control, "workflow_run", workflow_run)

    events._verify_provider(execution, "42", "Bearer token")

    workflow_run.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event", "pull_request"),
        ("display_title", "eng-platform-quality-untrusted"),
        ("path", ".github/workflows/untrusted.yml"),
    ],
)
def test_verify_provider_rejects_each_pr_workflow_identity_mismatch(
    monkeypatch, field, value
):
    execution = _execution(provider="github_actions")
    monkeypatch.setattr(
        events.release_workflow_identity,
        "verify",
        lambda *_: {"run_id": "42"},
    )
    monkeypatch.setattr(
        events.github_release_control,
        "workflow_run",
        lambda *_: _pr_workflow_run(execution, **{field: value}),
    )

    with pytest.raises(HTTPException) as error:
        events._verify_provider(execution, "42", "Bearer token")

    assert error.value.status_code == 403


def test_verify_event_token_accepts_only_matching_sha256():
    events._verify_event_token(_execution(), EVENT_TOKEN)


@pytest.mark.parametrize(
    ("token", "event_token_hash"),
    [
        (None, hashlib.sha256(EVENT_TOKEN.encode()).hexdigest()),
        ("", hashlib.sha256(EVENT_TOKEN.encode()).hexdigest()),
        ("wrong-token", hashlib.sha256(EVENT_TOKEN.encode()).hexdigest()),
        (EVENT_TOKEN, ""),
        (EVENT_TOKEN, "0" * 63),
        ("x" * 513, hashlib.sha256(("x" * 513).encode()).hexdigest()),
    ],
)
def test_verify_event_token_rejects_absent_wrong_or_unbound_token(
    token, event_token_hash
):
    with pytest.raises(HTTPException) as error:
        events._verify_event_token(_execution(event_token_hash=event_token_hash), token)

    assert error.value.status_code == 401
    assert error.value.detail == "Invalid execution event token"
    if token:
        assert token not in error.value.detail


def test_verify_report_is_optional():
    assert events._verify_report(_execution(), _event()) == ""


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("service_name", "other-service"),
        ("repository", "owner/other"),
        ("commit_sha", "c" * 40),
        ("base_sha", "d" * 40),
        ("policy_version", "oss-v1"),
    ],
)
def test_verify_report_requires_exact_release_identity(field, value):
    payload = _event(report=_report(**{field: value}))
    with pytest.raises(HTTPException) as error:
        events._verify_report(_execution(), payload)
    assert error.value.status_code == 403


def test_verify_report_stages_by_execution_and_expected_hash(monkeypatch):
    report = _report()
    save = mock.Mock(return_value="e" * 64)
    monkeypatch.setattr(events.quality_store, "save_pending_report", save)

    result = events._verify_report(
        _execution(), _event(report=report, report_hash="e" * 64)
    )

    assert result == "e" * 64
    save.assert_called_once_with("execution-1", report, expected_hash="e" * 64)


def test_verify_report_conflict_has_no_direct_execution_side_effect(monkeypatch):
    conflict = events.quality_store.QualityEvidenceConflict("conflicting report")
    monkeypatch.setattr(
        events.quality_store,
        "save_pending_report",
        mock.Mock(side_effect=conflict),
    )
    save = mock.Mock()
    monkeypatch.setattr(events.release_executions, "save", save)

    with pytest.raises(HTTPException) as error:
        events._verify_report(_execution(), _event(report=_report()))

    assert error.value.status_code == 409
    save.assert_not_called()


def test_verify_report_conflict_preserves_terminal_execution(monkeypatch):
    monkeypatch.setattr(
        events.quality_store,
        "save_pending_report",
        mock.Mock(side_effect=events.quality_store.QualityEvidenceConflict("conflict")),
    )
    monkeypatch.setattr(
        events.release_executions,
        "save",
        mock.Mock(side_effect=ValueError("terminal state")),
    )
    with pytest.raises(HTTPException) as error:
        events._verify_report(_execution(), _event(report=_report()))
    assert error.value.status_code == 409


def test_verify_plan_is_optional():
    assert events._verify_plan(_execution(), _event()) is None


def test_verify_plan_rejects_pr_plan():
    plan = ReleasePlan(
        next_version="1.2.3",
        git_tag="v1.2.3",
        release_type="patch",
        config_hash=PLANNER_HASH,
    )
    with pytest.raises(HTTPException) as error:
        events._verify_plan(_execution(), _event(release_plan=plan))
    assert error.value.status_code == 403


def test_verify_plan_requires_exact_planner_hash():
    plan = ReleasePlan(
        next_version="1.2.3",
        git_tag="v1.2.3",
        release_type="patch",
        config_hash="0" * 64,
    )
    with pytest.raises(HTTPException) as error:
        events._verify_plan(
            _execution(operation="main_release", planner_hash=PLANNER_HASH),
            _event(release_plan=plan),
        )
    assert error.value.status_code == 403


def test_verify_plan_rejects_tag_on_no_release():
    plan = ReleasePlan(
        next_version="",
        git_tag="v1.2.3",
        release_type="none",
        config_hash=PLANNER_HASH,
    )
    with pytest.raises(HTTPException) as error:
        events._verify_plan(
            _execution(operation="main_release", planner_hash=PLANNER_HASH),
            _event(release_plan=plan),
        )
    assert error.value.status_code == 422


@pytest.mark.parametrize(
    ("version", "tag"),
    [("1.2.3", "release-1.2.3"), ("1.2.3", "v1.2.4"), ("", "")],
)
def test_verify_plan_rejects_non_semantic_or_mismatched_release(version, tag):
    plan = ReleasePlan(
        next_version=version,
        git_tag=tag,
        release_type="patch",
        config_hash=PLANNER_HASH,
    )
    with pytest.raises(HTTPException) as error:
        events._verify_plan(
            _execution(operation="main_release", planner_hash=PLANNER_HASH),
            _event(release_plan=plan),
        )
    assert error.value.status_code == 422


@pytest.mark.parametrize(
    ("release_type", "version", "tag"),
    [
        ("none", "", ""),
        ("patch", "1.2.3", "v1.2.3"),
        ("minor", "2.0.0-beta.1", "v2.0.0-beta.1"),
    ],
)
def test_verify_plan_returns_validated_public_dict(release_type, version, tag):
    plan = ReleasePlan(
        next_version=version,
        git_tag=tag,
        release_type=release_type,
        notes="notes",
        config_hash=PLANNER_HASH,
    )
    result = events._verify_plan(
        _execution(operation="main_release", planner_hash=PLANNER_HASH),
        _event(release_plan=plan),
    )
    assert result["release_type"] == release_type
    assert result["git_tag"] == tag


@pytest.mark.parametrize(
    ("length", "status"), [("1000001", 413), ("not-a-number", 400)]
)
def test_accept_event_rejects_invalid_content_length(length, status):
    with pytest.raises(HTTPException) as error:
        events.accept_event(
            "execution-1", _event(), _request(length), authorization="Bearer token"
        )
    assert error.value.status_code == status


def test_accept_event_enforces_actual_body_limit_when_header_is_understated():
    with pytest.raises(HTTPException) as error:
        events.accept_event(
            "execution-1",
            _event(),
            _request("1", body=b"x" * 1_000_001),
            authorization="Bearer token",
        )
    assert error.value.status_code == 413


def test_accept_event_rejects_unknown_execution_or_payload_id(monkeypatch):
    monkeypatch.setattr(events.release_executions, "get", lambda _: None)
    with pytest.raises(HTTPException) as error:
        events.accept_event(
            "execution-1", _event(), _request(), authorization="Bearer token"
        )
    assert error.value.status_code == 404

    monkeypatch.setattr(events.release_executions, "get", lambda _: _execution())
    with pytest.raises(HTTPException) as error:
        events.accept_event(
            "execution-1",
            _event(execution_id="different"),
            _request(),
            authorization="Bearer token",
        )
    assert error.value.status_code == 404


def test_accept_event_rejects_fingerprint_before_provider_or_report(monkeypatch):
    monkeypatch.setattr(events.release_executions, "get", lambda _: _execution())
    provider = mock.Mock()
    pending = mock.Mock()
    monkeypatch.setattr(events, "_verify_provider", provider)
    monkeypatch.setattr(events.quality_store, "save_pending_report", pending)

    with pytest.raises(HTTPException) as error:
        events.accept_event(
            "execution-1",
            _event(fingerprint="0" * 64, report=_report()),
            _request(),
            authorization="Bearer token",
        )

    assert error.value.status_code == 403
    provider.assert_not_called()
    pending.assert_not_called()


@pytest.mark.parametrize(
    "event_token",
    [None, "", "wrong-token", "x" * 513],
)
def test_accept_event_requires_event_token_before_staging_or_state_change(
    monkeypatch, event_token
):
    monkeypatch.setattr(events.release_executions, "get", lambda _: _execution())
    provider = mock.Mock()
    pending = mock.Mock()
    accept = mock.Mock()
    reconcile = mock.Mock()
    monkeypatch.setattr(events, "_verify_provider", provider)
    monkeypatch.setattr(events.quality_store, "save_pending_report", pending)
    monkeypatch.setattr(events.release_executions, "accept_event", accept)
    monkeypatch.setattr(events.release_reconciler, "reconcile", reconcile)

    with pytest.raises(HTTPException) as error:
        events.accept_event(
            "execution-1",
            _event(status="running_quality"),
            _request(),
            authorization="Bearer token",
            x_eng_platform_event_token=event_token,
        )

    assert error.value.status_code == 401
    provider.assert_called_once()
    pending.assert_not_called()
    accept.assert_not_called()
    reconcile.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        _event(status="quality_passed"),
        _event(status="running_quality", report=_report()),
    ],
)
def test_accept_event_requires_report_exactly_for_quality_completion(
    monkeypatch, payload
):
    monkeypatch.setattr(events.release_executions, "get", lambda _: _execution())
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    with pytest.raises(HTTPException) as error:
        events.accept_event(
            "execution-1", payload, _request(), authorization="Bearer token"
        )
    assert error.value.status_code == 422
    assert "Quality completion" in error.value.detail


@pytest.mark.parametrize(
    "payload",
    [
        _event(status="release_planned"),
        _event(
            status="running_quality",
            release_plan=ReleasePlan(
                next_version="1.2.3",
                git_tag="v1.2.3",
                release_type="patch",
                config_hash=PLANNER_HASH,
            ),
        ),
    ],
)
def test_accept_event_requires_plan_exactly_for_release_completion(
    monkeypatch, payload
):
    execution = _execution(operation="main_release", planner_hash=PLANNER_HASH)
    monkeypatch.setattr(events.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    with pytest.raises(HTTPException) as error:
        events.accept_event(
            "execution-1", payload, _request(), authorization="Bearer token"
        )
    assert error.value.status_code == 422
    assert "Release completion" in error.value.detail


@pytest.mark.parametrize("sequence", [4, 5])
def test_stale_or_duplicate_callback_has_no_report_or_state_side_effects(
    monkeypatch, sequence
):
    execution = _execution(event_sequence=5, status="quality_passed")
    monkeypatch.setattr(events.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    pending = mock.Mock(
        side_effect=events.quality_store.QualityEvidenceConflict("must not execute")
    )
    save = mock.Mock()
    accept = mock.Mock()
    reconcile = mock.Mock()
    monkeypatch.setattr(events.quality_store, "save_pending_report", pending)
    monkeypatch.setattr(events.release_executions, "save", save)
    monkeypatch.setattr(events.release_executions, "accept_event", accept)
    monkeypatch.setattr(events.release_reconciler, "reconcile", reconcile)

    result = events.accept_event(
        "execution-1",
        _event(sequence=sequence, report=_report()),
        _request(),
        authorization="Bearer token",
    )

    assert result == {
        "accepted": False,
        "event_sequence": 5,
        "status": "quality_passed",
    }
    pending.assert_not_called()
    save.assert_not_called()
    accept.assert_not_called()
    reconcile.assert_not_called()


@pytest.mark.parametrize("terminal_conflict", [False, True])
def test_current_report_conflict_marks_unknown_but_tolerates_terminal_race(
    monkeypatch, terminal_conflict
):
    execution = _execution()
    monkeypatch.setattr(events.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    monkeypatch.setattr(
        events.quality_store,
        "save_pending_report",
        mock.Mock(
            side_effect=events.quality_store.QualityEvidenceConflict(
                "conflicting report"
            )
        ),
    )
    save = mock.Mock()
    if terminal_conflict:
        save.side_effect = ValueError("terminal")
    monkeypatch.setattr(events.release_executions, "save", save)

    with pytest.raises(HTTPException) as error:
        events.accept_event(
            "execution-1",
            _event(report=_report()),
            _request(),
            authorization="Bearer token",
        )

    assert error.value.status_code == 409
    save.assert_called_once_with(
        "execution-1", status="unknown", error="conflicting report"
    )


def test_current_nonconflict_validation_error_does_not_mark_unknown(monkeypatch):
    monkeypatch.setattr(events.release_executions, "get", lambda _: _execution())
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    save = mock.Mock()
    monkeypatch.setattr(events.release_executions, "save", save)

    with pytest.raises(HTTPException) as error:
        events.accept_event(
            "execution-1",
            _event(report=_report(repository="owner/other")),
            _request(),
            authorization="Bearer token",
        )

    assert error.value.status_code == 403
    save.assert_not_called()


def test_new_callback_stages_report_then_reconciles_after_monotonic_accept(
    monkeypatch,
):
    execution = _execution(event_sequence=1)
    monkeypatch.setattr(events.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    pending = mock.Mock(return_value="e" * 64)
    monkeypatch.setattr(events.quality_store, "save_pending_report", pending)
    accept = mock.Mock(
        return_value=(
            {**execution, "event_sequence": 2, "status": "running_quality"},
            True,
        )
    )
    monkeypatch.setattr(events.release_executions, "accept_event", accept)
    reconcile = mock.Mock(
        return_value={**execution, "event_sequence": 2, "status": "quality_passed"}
    )
    monkeypatch.setattr(events.release_reconciler, "reconcile", reconcile)

    result = events.accept_event(
        "execution-1",
        _event(sequence=2, report=_report(), report_hash="e" * 64),
        _request(),
        authorization="Bearer token",
    )

    assert result["event_sequence"] == 2
    assert result["status"] == "quality_passed"
    assert accept.call_args.kwargs["pending_report_hash"] == "e" * 64
    assert accept.call_args.kwargs["status"] == "running_quality"
    reconcile.assert_called_once_with("execution-1")


def test_release_callback_persists_validated_plan(monkeypatch):
    execution = _execution(operation="main_release", planner_hash=PLANNER_HASH)
    plan = ReleasePlan(
        next_version="1.2.3",
        git_tag="v1.2.3",
        release_type="patch",
        config_hash=PLANNER_HASH,
    )
    monkeypatch.setattr(events.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    accept = mock.Mock(
        return_value=(
            {**execution, "status": "running_quality", "event_sequence": 1},
            True,
        )
    )
    monkeypatch.setattr(events.release_executions, "accept_event", accept)
    monkeypatch.setattr(
        events.release_reconciler,
        "reconcile",
        lambda _: {**execution, "status": "released", "event_sequence": 1},
    )

    result = events.accept_event(
        "execution-1",
        _event(status="release_planned", release_plan=plan),
        _request(),
        authorization="Bearer token",
    )

    assert result["status"] == "released"
    assert accept.call_args.kwargs["release_plan"]["git_tag"] == "v1.2.3"


def test_callback_that_loses_monotonic_race_does_not_reconcile(monkeypatch):
    execution = _execution(event_sequence=0)
    monkeypatch.setattr(events.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    monkeypatch.setattr(events, "_verify_report", lambda *_: "")
    monkeypatch.setattr(events, "_verify_plan", lambda *_: None)
    monkeypatch.setattr(
        events.release_executions,
        "accept_event",
        lambda *args, **kwargs: (
            {**execution, "event_sequence": 2, "status": "running_quality"},
            False,
        ),
    )
    reconcile = mock.Mock()
    monkeypatch.setattr(events.release_reconciler, "reconcile", reconcile)

    result = events.accept_event(
        "execution-1",
        _event(status="running_quality"),
        _request(),
        authorization="Bearer token",
    )

    assert result["accepted"] is False
    assert result["event_sequence"] == 2
    reconcile.assert_not_called()


def test_failed_callback_is_accepted_without_reconciliation(monkeypatch):
    execution = _execution()
    monkeypatch.setattr(events.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    monkeypatch.setattr(events, "_verify_report", lambda *_: "")
    monkeypatch.setattr(events, "_verify_plan", lambda *_: None)
    monkeypatch.setattr(
        events.release_executions,
        "accept_event",
        lambda *args, **kwargs: (
            {**execution, "event_sequence": 1, "status": "failed"},
            True,
        ),
    )
    reconcile = mock.Mock()
    monkeypatch.setattr(events.release_reconciler, "reconcile", reconcile)

    result = events.accept_event(
        "execution-1",
        _event(status="failed", error="quality failed"),
        _request(),
        authorization="Bearer token",
    )

    assert result["status"] == "failed"
    reconcile.assert_not_called()


def test_release_failure_after_quality_event_remains_reconcilable(monkeypatch):
    state = _execution(
        operation="main_release",
        planner_hash=PLANNER_HASH,
        event_sequence=1,
    )
    monkeypatch.setattr(events.release_executions, "get", lambda _: dict(state))
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    monkeypatch.setattr(events, "_verify_report", lambda *_: "e" * 64)
    monkeypatch.setattr(events, "_verify_plan", lambda *_: None)

    def accept_event(execution_id, sequence, **changes):
        assert execution_id == state["execution_id"]
        assert sequence > state["event_sequence"]
        state.update(changes)
        state["event_sequence"] = sequence
        return dict(state), True

    monkeypatch.setattr(events.release_executions, "accept_event", accept_event)
    reconcile = mock.Mock(side_effect=lambda _: dict(state))
    monkeypatch.setattr(events.release_reconciler, "reconcile", reconcile)

    quality = events.accept_event(
        "execution-1",
        _event(
            sequence=2,
            status="quality_passed",
            report=_report(),
            report_hash="e" * 64,
        ),
        _request(),
        authorization="Bearer token",
    )
    failed_release = events.accept_event(
        "execution-1",
        _event(sequence=3, status="failed", error="planner crashed"),
        _request(),
        authorization="Bearer token",
    )

    assert quality == {
        "accepted": True,
        "event_sequence": 2,
        "status": "running_quality",
    }
    assert failed_release == {
        "accepted": True,
        "event_sequence": 3,
        "status": "running_quality",
    }
    assert state["pending_report_hash"] == "e" * 64
    assert state["engine_event_status"] == "failed"
    assert state["release_engine_failed"] is True
    assert state["release_error"] == "planner crashed"
    assert reconcile.call_args_list == [
        mock.call("execution-1"),
        mock.call("execution-1"),
    ]


def test_quality_phase_failure_is_terminal_without_staging_evidence(monkeypatch):
    execution = _execution(operation="main_release")
    monkeypatch.setattr(events.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    monkeypatch.setattr(events, "_verify_report", lambda *_: "")
    monkeypatch.setattr(events, "_verify_plan", lambda *_: None)
    accept = mock.Mock(
        return_value=(
            {**execution, "event_sequence": 1, "status": "failed"},
            True,
        )
    )
    monkeypatch.setattr(events.release_executions, "accept_event", accept)
    reconcile = mock.Mock()
    monkeypatch.setattr(events.release_reconciler, "reconcile", reconcile)

    result = events.accept_event(
        "execution-1",
        _event(status="failed", error="tests failed"),
        _request(),
        authorization="Bearer token",
    )

    assert result["status"] == "failed"
    assert accept.call_args.kwargs["status"] == "failed"
    assert "pending_report_hash" not in accept.call_args.kwargs
    assert "release_engine_failed" not in accept.call_args.kwargs
    reconcile.assert_not_called()


def test_accept_event_maps_invalid_transition_to_conflict(monkeypatch):
    monkeypatch.setattr(events.release_executions, "get", lambda _: _execution())
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    monkeypatch.setattr(events, "_verify_report", lambda *_: "")
    monkeypatch.setattr(events, "_verify_plan", lambda *_: None)
    monkeypatch.setattr(
        events.release_executions,
        "accept_event",
        mock.Mock(side_effect=ValueError("invalid transition")),
    )

    with pytest.raises(HTTPException) as error:
        events.accept_event(
            "execution-1",
            _event(status="running_quality"),
            _request(),
            authorization="Bearer token",
        )
    assert error.value.status_code == 409


@pytest.mark.parametrize(
    "reconcile_error",
    [
        ValueError("invalid evidence"),
        events.quality_store.QualityEvidenceConflict("conflict"),
    ],
)
def test_accept_event_marks_unknown_only_for_new_accepted_reconcile_failure(
    monkeypatch, reconcile_error
):
    execution = _execution()
    monkeypatch.setattr(events.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    monkeypatch.setattr(events, "_verify_report", lambda *_: "")
    monkeypatch.setattr(events, "_verify_plan", lambda *_: None)
    monkeypatch.setattr(
        events.release_executions,
        "accept_event",
        lambda *args, **kwargs: (
            {**execution, "event_sequence": 1, "status": "running_quality"},
            True,
        ),
    )
    monkeypatch.setattr(
        events.release_reconciler,
        "reconcile",
        mock.Mock(side_effect=reconcile_error),
    )
    save = mock.Mock(return_value={**execution, "status": "unknown"})
    monkeypatch.setattr(events.release_executions, "save", save)

    result = events.accept_event(
        "execution-1",
        _event(status="running_quality"),
        _request(),
        authorization="Bearer token",
    )

    assert result["status"] == "unknown"
    save.assert_called_once_with(
        "execution-1", status="unknown", error=str(reconcile_error)
    )


def test_resolve_requires_github_execution(monkeypatch):
    monkeypatch.setattr(events.release_executions, "find", lambda *_: None)
    with pytest.raises(HTTPException) as error:
        events.resolve_execution(
            events.ResolveExecutionRequest(
                repository="owner/repo", head_sha=HEAD, operation="pr_quality"
            ),
            authorization="Bearer token",
        )
    assert error.value.status_code == 404

    monkeypatch.setattr(
        events.release_executions, "find", lambda *_: _execution(provider="cloud_build")
    )
    with pytest.raises(HTTPException) as error:
        events.resolve_execution(
            events.ResolveExecutionRequest(
                repository="owner/repo", head_sha=HEAD, operation="pr_quality"
            ),
            authorization="Bearer token",
        )
    assert error.value.status_code == 404


def test_resolve_maps_invalid_workflow_identity(monkeypatch):
    monkeypatch.setattr(
        events.release_executions,
        "find",
        lambda *_: _execution(provider="github_actions"),
    )
    monkeypatch.setattr(
        events.release_workflow_identity,
        "verify",
        mock.Mock(
            side_effect=release_workflow_identity.ReleaseWorkflowIdentityError("bad")
        ),
    )
    with pytest.raises(HTTPException) as error:
        events.resolve_execution(
            events.ResolveExecutionRequest(
                repository="owner/repo", head_sha=HEAD, operation="pr_quality"
            ),
            authorization="Bearer token",
        )
    assert error.value.status_code == 401


def test_resolve_pr_verifies_run_identity_and_binds_run(monkeypatch):
    execution = _execution(provider="github_actions")
    monkeypatch.setattr(events.release_executions, "find", lambda *_: execution)
    monkeypatch.setattr(
        events.release_workflow_identity,
        "verify",
        lambda *_: {"run_id": "42"},
    )
    monkeypatch.setattr(
        events.github_release_control,
        "workflow_run",
        lambda *_: _pr_workflow_run(execution),
    )
    save = mock.Mock(
        return_value={**execution, "provider_run_id": "42", "github_run_id": 42}
    )
    monkeypatch.setattr(events.release_executions, "save", save)

    result = events.resolve_execution(
        events.ResolveExecutionRequest(
            repository=execution["repository"], head_sha=HEAD, operation="pr_quality"
        ),
        authorization="Bearer token",
    )

    assert result["execution_id"] == execution["execution_id"]
    assert result["executor_image"] == EXECUTOR_DIGEST
    assert result["planner_image"] == ""
    save.assert_called_once_with("execution-1", provider_run_id="42", github_run_id=42)


def test_resolve_pr_rejects_unreadable_or_wrong_identity_run(monkeypatch):
    execution = _execution(provider="github_actions")
    monkeypatch.setattr(events.release_executions, "find", lambda *_: execution)
    monkeypatch.setattr(
        events.release_workflow_identity,
        "verify",
        lambda *_: {"run_id": "42"},
    )
    monkeypatch.setattr(
        events.github_release_control,
        "workflow_run",
        mock.Mock(side_effect=RuntimeError("unavailable")),
    )
    request = events.ResolveExecutionRequest(
        repository=execution["repository"], head_sha=HEAD, operation="pr_quality"
    )
    with pytest.raises(HTTPException) as error:
        events.resolve_execution(request, authorization="Bearer token")
    assert error.value.status_code == 502

    monkeypatch.setattr(
        events.github_release_control,
        "workflow_run",
        lambda *_: _pr_workflow_run(execution, event="pull_request"),
    )
    with pytest.raises(HTTPException) as error:
        events.resolve_execution(request, authorization="Bearer token")
    assert error.value.status_code == 403


def test_resolve_rejects_different_existing_run(monkeypatch):
    execution = _execution(provider="github_actions", provider_run_id="41")
    monkeypatch.setattr(events.release_executions, "find", lambda *_: execution)
    monkeypatch.setattr(
        events.release_workflow_identity,
        "verify",
        lambda *_: {"run_id": "42"},
    )
    monkeypatch.setattr(
        events.github_release_control,
        "workflow_run",
        lambda *_: _pr_workflow_run(execution),
    )
    with pytest.raises(HTTPException) as error:
        events.resolve_execution(
            events.ResolveExecutionRequest(
                repository=execution["repository"],
                head_sha=HEAD,
                operation="pr_quality",
            ),
            authorization="Bearer token",
        )
    assert error.value.status_code == 409


def test_resolve_main_returns_pinned_planner_without_extra_run_lookup(
    monkeypatch,
):
    execution = _execution(
        operation="main_release",
        provider="github_actions",
        planner_hash=PLANNER_HASH,
    )
    monkeypatch.setattr(events.release_executions, "find", lambda *_: execution)
    monkeypatch.setattr(
        events.release_workflow_identity,
        "verify",
        lambda *_: {"run_id": "42"},
    )
    workflow_run = mock.Mock()
    monkeypatch.setattr(events.github_release_control, "workflow_run", workflow_run)
    monkeypatch.setattr(
        events.config.release_orchestrator,
        "release_planner_image",
        "planner@sha256:" + "e" * 64,
    )
    monkeypatch.setattr(
        events.release_executions,
        "save",
        lambda execution_id, **changes: {**execution, **changes},
    )

    result = events.resolve_execution(
        events.ResolveExecutionRequest(
            repository=execution["repository"], head_sha=HEAD, operation="main_release"
        ),
        authorization="Bearer token",
    )

    assert result["planner_hash"] == PLANNER_HASH
    assert result["planner_image"].startswith("planner@sha256:")
    workflow_run.assert_not_called()


def test_source_token_rejects_unknown_or_wrong_fingerprint(monkeypatch):
    monkeypatch.setattr(events.release_executions, "get", lambda _: None)
    request = events.SourceTokenRequest(
        fingerprint=FINGERPRINT, provider_run_id="build-1"
    )
    with pytest.raises(HTTPException) as error:
        events.issue_source_token("execution-1", request, authorization="Bearer token")
    assert error.value.status_code == 404

    monkeypatch.setattr(events.release_executions, "get", lambda _: _execution())
    wrong = events.SourceTokenRequest(fingerprint="0" * 64, provider_run_id="build-1")
    with pytest.raises(HTTPException) as error:
        events.issue_source_token("execution-1", wrong, authorization="Bearer token")
    assert error.value.status_code == 403


@pytest.mark.parametrize("provider_name", ["cloud_build", "github_actions"])
def test_source_token_is_one_time_after_verified_provider(monkeypatch, provider_name):
    execution = _execution(provider=provider_name)
    monkeypatch.setattr(events.release_executions, "get", lambda _: execution)
    provider = mock.Mock()
    monkeypatch.setattr(events, "_verify_provider", provider)
    monkeypatch.setattr(
        events.release_executions,
        "claim_source_token",
        lambda *_args, **_kwargs: True,
    )
    mint = mock.Mock(return_value=("installation-token", "2026-09-22T13:00:00Z"))
    monkeypatch.setattr(events.github_release_control, "installation_read_token", mint)
    request = events.SourceTokenRequest(
        fingerprint=FINGERPRINT, provider_run_id="build-1"
    )

    result = events.issue_source_token(
        "execution-1", request, authorization="Bearer oidc-token"
    )

    assert result == {
        "token": "installation-token",
        "expires_at": "2026-09-22T13:00:00Z",
    }
    provider.assert_called_once_with(execution, "build-1", "Bearer oidc-token")
    mint.assert_called_once_with(execution["repository"])


def test_source_token_rejects_reuse_before_minting(monkeypatch):
    monkeypatch.setattr(events.release_executions, "get", lambda _: _execution())
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    monkeypatch.setattr(
        events.release_executions,
        "claim_source_token",
        lambda *_args, **_kwargs: False,
    )
    mint = mock.Mock()
    monkeypatch.setattr(events.github_release_control, "installation_read_token", mint)

    with pytest.raises(HTTPException) as error:
        events.issue_source_token(
            "execution-1",
            events.SourceTokenRequest(
                fingerprint=FINGERPRINT, provider_run_id="build-1"
            ),
            authorization="Bearer token",
        )
    assert error.value.status_code == 409
    mint.assert_not_called()


def test_source_token_mint_failure_consumes_claim_and_hides_error(monkeypatch):
    monkeypatch.setattr(events.release_executions, "get", lambda _: _execution())
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    claim = mock.Mock(return_value=True)
    monkeypatch.setattr(events.release_executions, "claim_source_token", claim)
    monkeypatch.setattr(
        events.github_release_control,
        "installation_read_token",
        mock.Mock(side_effect=RuntimeError("secret provider response")),
    )

    with pytest.raises(HTTPException) as error:
        events.issue_source_token(
            "execution-1",
            events.SourceTokenRequest(
                fingerprint=FINGERPRINT, provider_run_id="build-1"
            ),
            authorization="Bearer token",
        )
    assert error.value.status_code == 502
    assert "secret" not in error.value.detail
    claim.assert_called_once_with("execution-1", provider_run_id="build-1")


def test_event_token_rejects_unknown_or_wrong_fingerprint_before_provider(
    monkeypatch,
):
    request = events.SourceTokenRequest(
        fingerprint=FINGERPRINT, provider_run_id="build-1"
    )
    provider = mock.Mock()
    claim = mock.Mock()
    monkeypatch.setattr(events, "_verify_provider", provider)
    monkeypatch.setattr(events.release_executions, "claim_event_token", claim)
    monkeypatch.setattr(events.release_executions, "get", lambda _: None)

    with pytest.raises(HTTPException) as error:
        events.issue_event_token(
            "execution-1", request, authorization="Bearer oidc-token"
        )
    assert error.value.status_code == 404

    monkeypatch.setattr(events.release_executions, "get", lambda _: _execution())
    wrong = events.SourceTokenRequest(fingerprint="0" * 64, provider_run_id="build-1")
    with pytest.raises(HTTPException) as error:
        events.issue_event_token(
            "execution-1", wrong, authorization="Bearer oidc-token"
        )
    assert error.value.status_code == 403
    provider.assert_not_called()
    claim.assert_not_called()


def test_event_token_verifies_provider_and_persists_only_sha256(monkeypatch):
    execution = _execution()
    execution.pop("event_token_hash")
    events.release_executions._memory[execution["execution_id"]] = dict(execution)
    provider = mock.Mock()
    generated = "raw-one-time-event-token"
    token_urlsafe = mock.Mock(return_value=generated)
    monkeypatch.setattr(events, "_verify_provider", provider)
    monkeypatch.setattr(events.secrets, "token_urlsafe", token_urlsafe)

    result = events.issue_event_token(
        execution["execution_id"],
        events.SourceTokenRequest(
            fingerprint=execution["fingerprint"],
            provider_run_id=execution["build_id"],
        ),
        authorization="Bearer oidc-token",
    )

    assert result == {"event_token": generated}
    provider.assert_called_once_with(execution, "build-1", "Bearer oidc-token")
    token_urlsafe.assert_called_once_with(32)
    stored = events.release_executions.get(execution["execution_id"])
    assert stored["event_token_hash"] == hashlib.sha256(generated.encode()).hexdigest()
    assert stored["event_token_issued_at"]
    assert "event_token" not in stored
    assert generated not in stored.values()


def test_event_token_reemission_is_rejected_without_replacing_first_hash(
    monkeypatch,
):
    execution = _execution()
    execution.pop("event_token_hash")
    events.release_executions._memory[execution["execution_id"]] = dict(execution)
    monkeypatch.setattr(events, "_verify_provider", mock.Mock())
    token_urlsafe = mock.Mock(side_effect=["first-event-token", "second-event-token"])
    monkeypatch.setattr(events.secrets, "token_urlsafe", token_urlsafe)
    request = events.SourceTokenRequest(
        fingerprint=execution["fingerprint"], provider_run_id=execution["build_id"]
    )

    assert events.issue_event_token(
        execution["execution_id"], request, authorization="Bearer oidc"
    ) == {"event_token": "first-event-token"}
    with pytest.raises(HTTPException) as error:
        events.issue_event_token(
            execution["execution_id"], request, authorization="Bearer oidc"
        )

    assert error.value.status_code == 409
    assert error.value.detail == "Event token was already issued"
    assert "second-event-token" not in error.value.detail
    stored = events.release_executions.get(execution["execution_id"])
    assert (
        stored["event_token_hash"] == hashlib.sha256(b"first-event-token").hexdigest()
    )
    assert "second-event-token" not in stored.values()


def test_event_token_provider_failure_prevents_generation_and_claim(monkeypatch):
    execution = _execution()
    execution.pop("event_token_hash")
    monkeypatch.setattr(events.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(
        events,
        "_verify_provider",
        mock.Mock(side_effect=HTTPException(status_code=401, detail="invalid")),
    )
    token_urlsafe = mock.Mock()
    claim = mock.Mock()
    monkeypatch.setattr(events.secrets, "token_urlsafe", token_urlsafe)
    monkeypatch.setattr(events.release_executions, "claim_event_token", claim)

    with pytest.raises(HTTPException) as error:
        events.issue_event_token(
            execution["execution_id"],
            events.SourceTokenRequest(
                fingerprint=execution["fingerprint"],
                provider_run_id=execution["build_id"],
            ),
            authorization="Bearer invalid",
        )

    assert error.value.status_code == 401
    token_urlsafe.assert_not_called()
    claim.assert_not_called()
