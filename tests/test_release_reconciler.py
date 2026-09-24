"""Unit tests for terminal-provider gating and idempotent release reconciliation."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from eng_platform_api.models import QualityCheck, QualityReportCreate
from eng_platform_api.services import release_executions as execution_store
from eng_platform_api.services import release_reconciler as reconciler


HEAD = "a" * 40
BASE = "b" * 40
FINGERPRINT = "f" * 64
EXECUTOR_DIGEST = "quality@sha256:" + "d" * 64
PLANNER_DIGEST = "planner@sha256:" + "e" * 64
PLANNER_HASH = "c" * 64
SERVICE_ACCOUNT = "quality@test-project.iam.gserviceaccount.com"
REPOSITORY_RESOURCE = (
    "projects/test-project/locations/us-central1/"
    "connections/github/repositories/eng-platform-web"
)


@pytest.fixture(autouse=True)
def configured_service_account(monkeypatch):
    monkeypatch.setattr(
        reconciler.config.release_orchestrator, "service_account", SERVICE_ACCOUNT
    )
    monkeypatch.setattr(
        reconciler.config.release_orchestrator,
        "release_planner_image",
        PLANNER_DIGEST,
    )
    monkeypatch.setattr(
        reconciler.config.cloud_build,
        "repositories",
        {"eng-platform-web": REPOSITORY_RESOURCE},
    )


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
        "policy_hash": "policy-hash",
        "executor_digest": EXECUTOR_DIGEST,
        "planner_hash": "",
        "provider": "cloud_build",
        "build_id": "build-1",
        "provider_run_id": "build-1",
        "status": "running_quality",
        "pending_report_hash": "e" * 64,
        "check_ids": {"quality": 10, "workflows": 11, "release": 12},
        "logs_url": "https://console/build-1",
    }
    value.update(overrides)
    if value["operation"] == "main_release" and not value["planner_hash"]:
        value["planner_hash"] = PLANNER_HASH
    return value


def _substitutions(execution: dict | None = None) -> dict:
    execution = execution or _execution()
    value = {
        "_EXECUTION_ID": execution["execution_id"],
        "_REQUEST_FINGERPRINT": execution["fingerprint"],
        "_SERVICE_NAME": execution["service_name"],
        "_REPOSITORY": execution["repository"],
        "_HEAD_SHA": execution["head_sha"],
        "_BASE_SHA": execution["base_sha"],
        "_EXECUTOR_DIGEST": execution["executor_digest"],
        "_OPERATION": execution["operation"],
        "_PROFILE_SHA256": execution["profile_hash"],
    }
    if execution["operation"] == "main_release":
        value.update(
            {
                "_PLANNER_SHA256": execution["planner_hash"],
                "_PLANNER_DIGEST": PLANNER_DIGEST,
            }
        )
    return value


def _build(execution: dict | None = None, **overrides) -> dict:
    execution = execution or _execution()
    value = {
        "id": execution["build_id"],
        "status": "SUCCESS",
        "serviceAccount": SERVICE_ACCOUNT,
        "substitutions": _substitutions(execution),
        "source": {
            "connectedRepository": {
                "repository": REPOSITORY_RESOURCE,
                "revision": execution["head_sha"],
            }
        },
        "startTime": "2026-09-22T12:00:00Z",
        "finishTime": "2026-09-22T12:02:30Z",
    }
    value.update(overrides)
    return value


def _report(**overrides) -> QualityReportCreate:
    value = {
        "service_name": "eng-platform-web",
        "repository": "owner/eng-platform-web",
        "commit_sha": HEAD,
        "base_sha": BASE,
        "branch": "main",
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
                name="tests",
                category="tests",
                status="PASSED",
            )
        ],
    }
    value.update(overrides)
    return QualityReportCreate(**value)


def _save_merged(state: dict):
    def save(execution_id, **changes):
        assert execution_id == state["execution_id"]
        state.update(changes)
        return dict(state)

    return save


def test_complete_check_is_noop_without_bound_check(monkeypatch):
    upsert = mock.Mock()
    monkeypatch.setattr(reconciler.github_release_control, "upsert_check", upsert)

    reconciler._complete_check(_execution(check_ids={}), "quality", "success", "ok")

    upsert.assert_not_called()


def test_complete_check_updates_existing_check_without_creating_another(monkeypatch):
    upsert = mock.Mock()
    monkeypatch.setattr(reconciler.github_release_control, "upsert_check", upsert)
    execution = _execution()

    reconciler._complete_check(execution, "quality", "success", "passed")

    upsert.assert_called_once_with(
        repository=execution["repository"],
        head_sha=HEAD,
        kind="quality",
        status="completed",
        conclusion="success",
        details_url=execution["logs_url"],
        summary="passed",
        check_run_id=10,
    )


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
def test_verify_build_requires_every_exact_substitution(key, mode):
    build = _build()
    if mode == "missing":
        del build["substitutions"][key]
    else:
        build["substitutions"][key] = "wrong"

    with pytest.raises(ValueError, match="identity does not match"):
        reconciler._verify_build(_execution(), build)


def test_verify_build_accepts_exact_substitutions():
    reconciler._verify_build(_execution(), _build())


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
def test_verify_build_rejects_connected_repository_source_mismatch(source):
    with pytest.raises(ValueError, match="source does not match"):
        reconciler._verify_build(_execution(), _build(source=source))


@pytest.mark.parametrize(
    "account", [None, "", "other@test-project.iam.gserviceaccount.com"]
)
def test_verify_build_requires_exact_service_account(account):
    with pytest.raises(ValueError, match="service account does not match"):
        reconciler._verify_build(_execution(), _build(serviceAccount=account))


@pytest.mark.parametrize(
    "build",
    [
        {},
        {"startTime": "2026-09-22T12:00:00Z"},
        {
            "startTime": "invalid",
            "finishTime": "2026-09-22T12:00:00Z",
        },
    ],
)
def test_record_build_timing_ignores_missing_or_invalid_timestamps(monkeypatch, build):
    save = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    reconciler._record_build_timing("execution-1", build)
    save.assert_not_called()


def test_record_build_timing_records_bounded_seconds_and_minutes(monkeypatch):
    save = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "save", save)

    reconciler._record_build_timing("execution-1", _build())

    save.assert_called_once_with(
        "execution-1",
        build_duration_seconds=150.0,
        build_queue_seconds=0.0,
        build_attempts=[
            {
                "build_id": "build-1",
                "duration_seconds": 150.0,
                "queue_seconds": 0.0,
                "minutes": 2.5,
                "estimated_cost_usd": 0.015,
                "finished_at": "2026-09-22T12:02:30Z",
            }
        ],
        build_minutes_estimate=2.5,
        estimated_compute_cost_usd=0.015,
        build_minute_price_usd=0.006,
        cost_category="release_quality",
        build_finished_at="2026-09-22T12:02:30Z",
    )


def test_record_build_timing_clamps_clock_skew_to_zero(monkeypatch):
    save = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    reconciler._record_build_timing(
        "execution-1",
        {
            "startTime": "2026-09-22T12:01:00Z",
            "finishTime": "2026-09-22T12:00:00Z",
        },
    )
    save.assert_called_once_with(
        "execution-1",
        build_duration_seconds=0.0,
        build_queue_seconds=0.0,
        build_attempts=[
            {
                "build_id": "",
                "duration_seconds": 0.0,
                "queue_seconds": 0.0,
                "minutes": 0.0,
                "estimated_cost_usd": 0.0,
                "finished_at": "2026-09-22T12:00:00Z",
            }
        ],
        build_minutes_estimate=0.0,
        estimated_compute_cost_usd=0.0,
        build_minute_price_usd=0.006,
        cost_category="release_quality",
        build_finished_at="2026-09-22T12:00:00Z",
    )


def test_record_build_timing_accumulates_distinct_retry_attempts(monkeypatch):
    prior = {
        **_execution(),
        "build_attempts": [
            {
                "build_id": "build-1",
                "duration_seconds": 150.0,
                "queue_seconds": 0.0,
                "minutes": 2.5,
                "estimated_cost_usd": 0.015,
                "finished_at": "2026-09-22T12:02:30Z",
            }
        ],
    }
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: prior)
    save = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    retry = {
        **_build(),
        "id": "build-2",
        "startTime": "2026-09-22T12:02:30Z",
        "finishTime": "2026-09-22T12:03:00Z",
    }

    reconciler._record_build_timing("execution-1", retry)

    assert save.call_args.kwargs["build_minutes_estimate"] == 3.0
    assert save.call_args.kwargs["estimated_compute_cost_usd"] == 0.018
    assert [item["build_id"] for item in save.call_args.kwargs["build_attempts"]] == [
        "build-1",
        "build-2",
    ]


@pytest.mark.parametrize(
    ("created", "queue_seconds"),
    [
        ("2026-09-22T11:59:30Z", 30.0),
        ("invalid", 0.0),
        ("2026-09-22T12:01:00Z", 0.0),
    ],
)
def test_record_build_timing_handles_queue_timestamps(
    monkeypatch, created, queue_seconds
):
    save = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    monkeypatch.setattr(
        reconciler.release_executions, "monthly_cloud_build_minutes", lambda _: 0
    )

    reconciler._record_build_timing("execution-1", {**_build(), "createTime": created})

    assert save.call_args.kwargs["build_queue_seconds"] == queue_seconds


def test_record_build_timing_emits_each_usage_alert_only_after_atomic_claim(
    monkeypatch,
):
    monkeypatch.setattr(reconciler.release_executions, "save", mock.Mock())
    monkeypatch.setattr(
        reconciler.release_executions,
        "monthly_cloud_build_minutes",
        lambda month: 2250.0,
    )
    monkeypatch.setattr(
        reconciler.release_executions,
        "get",
        lambda _: {"repository": "repository-owner/private"},
    )
    monkeypatch.setattr(reconciler.config.github, "billing_owner", "billing-owner")
    monkeypatch.setattr(
        reconciler.config.release_orchestrator,
        "usage_alert_minutes",
        (2000, 2250, 2500),
    )
    claim = mock.Mock(side_effect=lambda owner, **kwargs: kwargs["threshold"] == 2250)
    warning = mock.Mock()
    monkeypatch.setattr(reconciler.executor_circuits, "claim_usage_alert", claim)
    monkeypatch.setattr(reconciler.logger, "warning", warning)

    reconciler._record_build_timing("execution-1", _build())

    assert claim.call_args_list == [
        mock.call(
            "billing-owner",
            month="2026-09",
            threshold=2000,
            observed_minutes=2250.0,
        ),
        mock.call(
            "billing-owner",
            month="2026-09",
            threshold=2250,
            observed_minutes=2250.0,
        ),
    ]
    warning.assert_called_once_with(
        "cloud_build_usage_threshold month=%s threshold_minutes=%s observed_minutes=%.3f",
        "2026-09",
        2250,
        2250.0,
    )


@pytest.mark.parametrize("terminal", [False, True])
def test_github_provider_requires_explicit_terminal_success(terminal):
    assert (
        reconciler._provider_success(
            _execution(provider="github_actions", provider_terminal_success=terminal)
        )
        is terminal
    )


def test_cloud_provider_without_bound_build_is_not_terminal(monkeypatch):
    execution = _execution(build_id="")
    get_build = mock.Mock()
    monkeypatch.setattr(reconciler.release_cloud_build, "get_build", get_build)
    monkeypatch.setattr(reconciler.catalog, "get_service", lambda _: object())
    recover = mock.Mock(return_value=execution)
    monkeypatch.setattr(
        reconciler.release_cloud_build, "reconcile_uncertain_submission", recover
    )

    assert reconciler._provider_success(execution) is False
    recover.assert_called_once_with("execution-1", mock.ANY)
    get_build.assert_not_called()


def test_cloud_provider_without_build_rejects_missing_catalog_service(monkeypatch):
    monkeypatch.setattr(reconciler.catalog, "get_service", lambda _: None)
    with pytest.raises(ValueError, match="service disappeared"):
        reconciler._provider_success(_execution(build_id=""))


def test_cloud_provider_binds_recovered_uncertain_build_then_verifies_it(monkeypatch):
    execution = _execution(build_id="")
    recovered = {**execution, "build_id": "recovered-build"}
    build = _build(id="recovered-build")
    monkeypatch.setattr(reconciler.catalog, "get_service", lambda _: object())
    monkeypatch.setattr(
        reconciler.release_cloud_build,
        "reconcile_uncertain_submission",
        lambda *_: recovered,
    )
    get_build = mock.Mock(return_value=build)
    monkeypatch.setattr(reconciler.release_cloud_build, "get_build", get_build)
    monkeypatch.setattr(reconciler, "_record_build_timing", mock.Mock())

    assert reconciler._provider_success(execution) is True
    get_build.assert_called_once_with("recovered-build")


def test_cloud_provider_working_does_not_commit_or_mutate(monkeypatch):
    execution = _execution()
    monkeypatch.setattr(
        reconciler.release_cloud_build,
        "get_build",
        lambda _: _build(status="WORKING"),
    )
    save = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "save", save)

    assert reconciler._provider_success(execution) is False
    save.assert_not_called()


@pytest.mark.parametrize(
    "state", ["FAILURE", "TIMEOUT", "CANCELLED", "EXPIRED", "INTERNAL_ERROR"]
)
def test_failed_cloud_provider_marks_execution_and_checks_failed(monkeypatch, state):
    execution = _execution()
    monkeypatch.setattr(
        reconciler.release_cloud_build,
        "get_build",
        lambda _: _build(status=state),
    )
    save = mock.Mock(return_value={**execution, "status": "failed"})
    complete = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    monkeypatch.setattr(reconciler, "_complete_check", complete)

    assert reconciler._provider_success(execution) is False
    save.assert_called_once_with(
        "execution-1",
        status="failed",
        provider_status=state,
        error=f"Quality build concluded {state}",
    )
    assert complete.call_args_list == [
        mock.call(execution, "quality", "failure", f"Quality build: {state}"),
        mock.call(execution, "workflows", "failure", f"Quality build: {state}"),
    ]


def test_quality_failed_event_on_failed_build_is_terminal_quality_output(
    monkeypatch,
):
    execution = _execution(
        engine_event_status="quality_failed", pending_report_hash="e" * 64
    )
    build = _build(status="FAILURE")
    monkeypatch.setattr(reconciler.release_cloud_build, "get_build", lambda _: build)
    timing = mock.Mock()
    save = mock.Mock()
    monkeypatch.setattr(reconciler, "_record_build_timing", timing)
    monkeypatch.setattr(reconciler.release_executions, "save", save)

    assert reconciler._provider_success(execution) is True
    timing.assert_called_once_with("execution-1", build)
    save.assert_not_called()


def test_cloud_build_release_failure_commits_quality_then_fails_only_release(
    monkeypatch,
):
    state = _execution(
        operation="main_release",
        engine_event_status="quality_passed",
        release_error="planner crashed",
    )
    report = _report()
    transitions = []

    monkeypatch.setattr(
        reconciler.release_cloud_build,
        "get_build",
        lambda _: _build(state, status="FAILURE"),
    )
    monkeypatch.setattr(reconciler, "_record_build_timing", mock.Mock())
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: dict(state))

    def save(execution_id, **changes):
        assert execution_id == state["execution_id"]
        state.update(changes)
        if "status" in changes:
            transitions.append(changes["status"])
        return dict(state)

    monkeypatch.setattr(reconciler.release_executions, "save", save)
    monkeypatch.setattr(
        reconciler.quality_store, "get_pending_report", lambda *_: report
    )

    def save_evidence(*args, **kwargs):
        transitions.append("evidence_committed")
        return SimpleNamespace(
            quality_gate_status="PASSED",
            received_at="2026-09-22T12:00:00+00:00",
        ), "e" * 64

    immutable = mock.Mock(side_effect=save_evidence)
    monkeypatch.setattr(reconciler.quality_store, "save_immutable_report", immutable)
    summary = mock.Mock()
    monkeypatch.setattr(reconciler.quality_store, "save_execution_summary", summary)
    monkeypatch.setattr(reconciler.catalog, "get_service", lambda _: object())
    monkeypatch.setattr(reconciler, "policy_errors", lambda *_: [])
    complete = mock.Mock()
    monkeypatch.setattr(reconciler, "_complete_check", complete)
    publish = mock.Mock()
    monkeypatch.setattr(reconciler, "_publish_if_allowed", publish)

    result = reconciler.reconcile("execution-1")

    assert result["status"] == "failed"
    assert result["evidence_committed"] is True
    assert result["report_hash"] == "e" * 64
    assert result["release_engine_failed"] is True
    assert result["error"] == "planner crashed"
    assert transitions == ["evidence_committed", "quality_passed", "failed"]
    immutable.assert_called_once()
    summary.assert_called_once()
    assert summary.call_args.args[0]["operation"] == "main_release"
    assert summary.call_args.args[2] == "e" * 64
    publish.assert_not_called()
    assert complete.call_args_list[-1].args[1:] == (
        "release",
        "failure",
        "planner crashed",
    )


def test_terminal_planner_only_failure_submits_one_cost_limited_retry(monkeypatch):
    state = _execution(
        operation="main_release",
        status="failed",
        engine_event_status="quality_passed",
        release_engine_failed=True,
        evidence_committed=True,
        report_hash="e" * 64,
        planner_retry_count=0,
    )
    build = _build(
        state,
        status="FAILURE",
        steps=[{"id": "release-plan", "status": "FAILURE"}],
    )
    staged = {
        **state,
        "status": "submission_pending",
        "build_id": "",
        "planner_retry_count": 1,
        "planner_retry_pending": True,
    }
    submitted = {**staged, "build_id": "planner-retry-build"}
    stage = mock.Mock(return_value=staged)
    submit = mock.Mock(return_value=submitted)
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: state)
    monkeypatch.setattr(
        reconciler.release_executions,
        "planner_retry_candidate",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(reconciler.release_cloud_build, "get_build", lambda _: build)
    monkeypatch.setattr(reconciler, "_record_build_timing", mock.Mock())
    monkeypatch.setattr(reconciler.release_executions, "stage_planner_retry", stage)
    monkeypatch.setattr(reconciler.catalog, "get_service", lambda _: object())
    monkeypatch.setattr(reconciler.release_cloud_build, "submit", submit)

    result = reconciler.reconcile("execution-1")

    assert result["build_id"] == "planner-retry-build"
    stage.assert_called_once_with(
        "execution-1",
        failed_build_id="build-1",
        planner_image=PLANNER_DIGEST,
        planner_hash_value=mock.ANY,
        remediation=False,
        previous_planner_image="",
    )
    submit.assert_called_once_with("execution-1", mock.ANY)


def test_terminal_failure_in_other_step_is_not_retried(monkeypatch):
    state = _execution(
        operation="main_release",
        status="failed",
        engine_event_status="quality_passed",
        release_engine_failed=True,
        evidence_committed=True,
        report_hash="e" * 64,
    )
    build = _build(
        state,
        status="FAILURE",
        steps=[{"id": "prepare", "status": "FAILURE"}],
    )
    save = mock.Mock(return_value={**state, "planner_retry_checked": True})
    stage = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: state)
    monkeypatch.setattr(
        reconciler.release_executions,
        "planner_retry_candidate",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(reconciler.release_cloud_build, "get_build", lambda _: build)
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    monkeypatch.setattr(reconciler.release_executions, "stage_planner_retry", stage)

    result = reconciler.reconcile("execution-1")

    assert result["planner_retry_checked"] is True
    stage.assert_not_called()


def test_verified_planner_image_change_allows_exactly_one_short_remediation(
    monkeypatch,
):
    old_image = PLANNER_DIGEST
    new_image = "planner@sha256:" + "f" * 64
    state = _execution(
        operation="main_release",
        status="failed",
        build_id="planner-retry-build-1",
        provider_run_id="planner-retry-build-1",
        engine_event_status="quality_passed",
        release_engine_failed=True,
        evidence_committed=True,
        report_hash="e" * 64,
        planner_retry_count=1,
        planner_retry_checked=True,
        planner_retry_pending=False,
        planner_retry_image=old_image,
    )
    substitutions = _substitutions(state)
    substitutions.update({"_PLANNER_DIGEST": old_image, "_PLANNER_RETRY_ATTEMPT": "1"})
    build = _build(
        state,
        status="FAILURE",
        substitutions=substitutions,
        steps=[{"id": "release-plan", "status": "FAILURE"}],
    )
    staged = {
        **state,
        "status": "submission_pending",
        "build_id": "",
        "planner_retry_count": 2,
        "planner_retry_pending": True,
        "planner_remediation_retry_pending": True,
        "planner_remediation_retry_image": new_image,
    }
    submitted = {**staged, "build_id": "planner-retry-build-2"}
    monkeypatch.setattr(
        reconciler.config.release_orchestrator, "release_planner_image", new_image
    )
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: state)
    monkeypatch.setattr(reconciler.release_cloud_build, "get_build", lambda _: build)
    monkeypatch.setattr(reconciler, "_record_build_timing", mock.Mock())
    stage = mock.Mock(return_value=staged)
    submit = mock.Mock(return_value=submitted)
    monkeypatch.setattr(reconciler.release_executions, "stage_planner_retry", stage)
    monkeypatch.setattr(reconciler.catalog, "get_service", lambda _: object())
    monkeypatch.setattr(reconciler.release_cloud_build, "submit", submit)

    result = reconciler.reconcile("execution-1")

    assert result["build_id"] == "planner-retry-build-2"
    stage.assert_called_once_with(
        "execution-1",
        failed_build_id="planner-retry-build-1",
        planner_image=new_image,
        planner_hash_value=reconciler.planner_hash(),
        remediation=True,
        previous_planner_image=old_image,
    )
    submit.assert_called_once_with("execution-1", mock.ANY)


def test_unknown_callback_image_drift_allows_only_verified_planner_remediation(
    monkeypatch,
):
    new_image = "planner@sha256:" + "f" * 64
    state = _execution(
        operation="main_release",
        status="unknown",
        error="Cloud Build release identity does not match execution",
        build_id="planner-retry-build-1",
        provider_run_id="planner-retry-build-1",
        engine_event_status="quality_passed",
        release_engine_failed=True,
        evidence_committed=True,
        report_hash="e" * 64,
        planner_retry_count=1,
        planner_retry_checked=True,
        planner_retry_pending=False,
        release_failed_step="release-plan",
    )
    substitutions = _substitutions(state)
    substitutions.update(
        {
            "_PLANNER_DIGEST": PLANNER_DIGEST,
            "_PLANNER_RETRY_ATTEMPT": "1",
        }
    )
    build = _build(
        state,
        status="FAILURE",
        substitutions=substitutions,
        steps=[{"id": "release-plan", "status": "FAILURE"}],
    )
    staged = {
        **state,
        "status": "submission_pending",
        "build_id": "",
        "planner_retry_count": 2,
        "planner_retry_pending": True,
        "planner_remediation_retry_pending": True,
        "planner_remediation_retry_image": new_image,
    }
    submitted = {**staged, "build_id": "planner-remediation-build"}

    monkeypatch.setattr(
        reconciler.config.release_orchestrator,
        "release_planner_image",
        new_image,
    )
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: state)
    monkeypatch.setattr(reconciler.release_cloud_build, "get_build", lambda _: build)
    monkeypatch.setattr(reconciler, "_record_build_timing", mock.Mock())
    monkeypatch.setattr(
        reconciler.release_executions,
        "save",
        lambda _execution_id, **changes: state.update(changes) or dict(state),
    )
    stage = mock.Mock(return_value=staged)
    monkeypatch.setattr(reconciler.release_executions, "stage_planner_retry", stage)
    monkeypatch.setattr(reconciler.catalog, "get_service", lambda _: object())
    submit = mock.Mock(return_value=submitted)
    monkeypatch.setattr(reconciler.release_cloud_build, "submit", submit)

    result = reconciler.reconcile("execution-1")

    assert state["status"] == "failed"
    assert state["error"] == (
        "Verified planner-only failure; callback image identity drift reconciled"
    )
    assert result["build_id"] == "planner-remediation-build"
    stage.assert_called_once_with(
        "execution-1",
        failed_build_id="planner-retry-build-1",
        planner_image=new_image,
        planner_hash_value=mock.ANY,
        remediation=True,
        previous_planner_image=PLANNER_DIGEST,
    )
    submit.assert_called_once_with("execution-1", mock.ANY)


def test_unknown_callback_drift_does_not_retry_a_non_planner_failure(monkeypatch):
    new_image = "planner@sha256:" + "f" * 64
    state = _execution(
        operation="main_release",
        status="unknown",
        error="Cloud Build release identity does not match execution",
        build_id="planner-retry-build-1",
        provider_run_id="planner-retry-build-1",
        engine_event_status="quality_passed",
        release_engine_failed=True,
        evidence_committed=True,
        report_hash="e" * 64,
        planner_retry_count=1,
        planner_retry_checked=True,
    )
    substitutions = _substitutions(state)
    substitutions.update(
        {
            "_PLANNER_DIGEST": PLANNER_DIGEST,
            "_PLANNER_RETRY_ATTEMPT": "1",
        }
    )
    build = _build(
        state,
        status="FAILURE",
        substitutions=substitutions,
        steps=[{"id": "prepare", "status": "FAILURE"}],
    )
    save = mock.Mock()
    submit = mock.Mock()
    monkeypatch.setattr(
        reconciler.config.release_orchestrator,
        "release_planner_image",
        new_image,
    )
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: state)
    monkeypatch.setattr(reconciler.release_cloud_build, "get_build", lambda _: build)
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    monkeypatch.setattr(reconciler.release_cloud_build, "submit", submit)

    result = reconciler.reconcile("execution-1")

    assert result is state
    save.assert_not_called()
    submit.assert_not_called()


def test_unknown_callback_drift_without_committed_hash_is_not_inspected(monkeypatch):
    state = _execution(
        operation="main_release",
        status="unknown",
        error="Cloud Build release identity does not match execution",
        engine_event_status="quality_passed",
        release_engine_failed=True,
        evidence_committed=True,
        planner_retry_count=1,
        planner_retry_checked=True,
        build_id="planner-retry-build-1",
    )
    get_build = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: state)
    monkeypatch.setattr(reconciler.release_cloud_build, "get_build", get_build)

    result = reconciler.reconcile("execution-1")

    assert result is state
    get_build.assert_not_called()


def test_unknown_callback_drift_keeps_execution_when_old_build_uses_current_image(
    monkeypatch,
):
    state = _execution(
        operation="main_release",
        status="unknown",
        error="Cloud Build release identity does not match execution",
        build_id="planner-retry-build-1",
        provider_run_id="planner-retry-build-1",
        engine_event_status="quality_passed",
        release_engine_failed=True,
        evidence_committed=True,
        report_hash="e" * 64,
        planner_retry_count=1,
        planner_retry_checked=True,
        planner_retry_pending=False,
    )
    substitutions = _substitutions(state)
    substitutions["_PLANNER_RETRY_ATTEMPT"] = "1"
    build = _build(
        state,
        status="FAILURE",
        substitutions=substitutions,
        steps=[{"id": "release-plan", "status": "FAILURE"}],
    )
    save = mock.Mock()
    submit = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: state)
    monkeypatch.setattr(reconciler.release_cloud_build, "get_build", lambda _: build)
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    monkeypatch.setattr(reconciler.release_cloud_build, "submit", submit)

    result = reconciler.reconcile("execution-1")

    assert result is state
    save.assert_not_called()
    submit.assert_not_called()


def test_cloud_build_failure_before_quality_is_terminal_without_evidence(
    monkeypatch,
):
    state = _execution(
        operation="main_release",
        pending_report_hash="",
        engine_event_status="running_quality",
    )
    monkeypatch.setattr(
        reconciler.release_cloud_build,
        "get_build",
        lambda _: _build(state, status="FAILURE"),
    )
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: dict(state))

    def save(execution_id, **changes):
        assert execution_id == state["execution_id"]
        state.update(changes)
        return dict(state)

    monkeypatch.setattr(reconciler.release_executions, "save", save)
    immutable = mock.Mock()
    monkeypatch.setattr(reconciler.quality_store, "save_immutable_report", immutable)
    complete = mock.Mock()
    monkeypatch.setattr(reconciler, "_complete_check", complete)

    result = reconciler.reconcile("execution-1")

    assert result["status"] == "failed"
    assert result["error"] == "Quality build concluded FAILURE"
    assert not result.get("evidence_committed")
    immutable.assert_not_called()
    assert [call.args[1:] for call in complete.call_args_list] == [
        ("quality", "failure", "Quality build: FAILURE"),
        ("workflows", "failure", "Quality build: FAILURE"),
    ]


def test_successful_cloud_provider_verifies_identity_and_records_timing(monkeypatch):
    execution = _execution()
    build = _build()
    monkeypatch.setattr(reconciler.release_cloud_build, "get_build", lambda _: build)
    verify = mock.Mock()
    timing = mock.Mock()
    monkeypatch.setattr(reconciler, "_verify_build", verify)
    monkeypatch.setattr(reconciler, "_record_build_timing", timing)

    assert reconciler._provider_success(execution) is True
    verify.assert_called_once_with(execution, build)
    timing.assert_called_once_with("execution-1", build)


def test_commit_quality_requires_pending_hash():
    with pytest.raises(ValueError, match="did not produce"):
        reconciler._commit_quality(_execution(pending_report_hash=""))


def test_commit_quality_requires_pending_report(monkeypatch):
    monkeypatch.setattr(reconciler.quality_store, "get_pending_report", lambda *_: None)
    with pytest.raises(ValueError, match="Pending quality report is missing"):
        reconciler._commit_quality(_execution())


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
def test_commit_quality_requires_exact_pending_report_identity(
    monkeypatch, field, value
):
    monkeypatch.setattr(
        reconciler.quality_store,
        "get_pending_report",
        lambda *_: _report(**{field: value}),
    )
    with pytest.raises(ValueError, match="does not match release execution"):
        reconciler._commit_quality(_execution())


def test_commit_quality_passes_complete_provenance_to_immutable_store(monkeypatch):
    execution = _execution()
    report = _report()
    stored = SimpleNamespace(
        quality_gate_status="PASSED",
        received_at="2026-09-22T12:00:00+00:00",
    )
    monkeypatch.setattr(
        reconciler.quality_store, "get_pending_report", lambda *_: report
    )
    immutable = mock.Mock(return_value=(stored, "committed-hash"))
    monkeypatch.setattr(reconciler.quality_store, "save_immutable_report", immutable)
    summary = mock.Mock()
    monkeypatch.setattr(reconciler.quality_store, "save_execution_summary", summary)
    monkeypatch.setattr(reconciler.catalog, "get_service", lambda _: object())
    monkeypatch.setattr(reconciler, "policy_errors", lambda *_: [])
    save = mock.Mock(return_value={**execution, "status": "quality_passed"})
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    complete = mock.Mock()
    monkeypatch.setattr(reconciler, "_complete_check", complete)

    result = reconciler._commit_quality(execution)

    assert result["status"] == "quality_passed"
    immutable.assert_called_once_with(
        report,
        fingerprint=FINGERPRINT,
        provider="cloud_build",
        provider_run_id="build-1",
        executor_digest=EXECUTOR_DIGEST,
        profile_hash="profile-hash",
        policy_hash="policy-hash",
        operation="pr_quality",
        expected_hash="e" * 64,
    )
    summary.assert_called_once_with(execution, stored, "committed-hash")
    save.assert_called_once_with(
        "execution-1",
        status="quality_passed",
        report_hash="committed-hash",
        evidence_committed=True,
    )
    assert complete.call_args_list == [
        mock.call(result, "quality", "success", "Exact oss-v2 evidence passed."),
        mock.call(result, "workflows", "success", "Service contracts passed."),
    ]


@pytest.mark.parametrize(
    ("gate", "errors"),
    [("FAILED", []), ("PASSED", ["differential coverage failed"])],
)
def test_commit_quality_marks_policy_failure_and_checks(monkeypatch, gate, errors):
    execution = _execution()
    stored = SimpleNamespace(
        quality_gate_status=gate,
        received_at="2026-09-22T12:00:00+00:00",
    )
    monkeypatch.setattr(
        reconciler.quality_store, "get_pending_report", lambda *_: _report()
    )
    monkeypatch.setattr(
        reconciler.quality_store,
        "save_immutable_report",
        lambda *args, **kwargs: (stored, "committed-hash"),
    )
    summary = mock.Mock()
    monkeypatch.setattr(reconciler.quality_store, "save_execution_summary", summary)
    monkeypatch.setattr(reconciler.catalog, "get_service", lambda _: object())
    monkeypatch.setattr(reconciler, "policy_errors", lambda *_: errors)
    save = mock.Mock(return_value={**execution, "status": "quality_failed"})
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    complete = mock.Mock()
    monkeypatch.setattr(reconciler, "_complete_check", complete)

    result = reconciler._commit_quality(execution)

    assert result["status"] == "quality_failed"
    summary.assert_called_once_with(execution, stored, "committed-hash")
    save.assert_called_once_with(
        "execution-1",
        status="quality_failed",
        report_hash="committed-hash",
        quality_errors=errors,
    )
    assert complete.call_count == 2


def test_publish_waits_for_canary_approval_or_auto_enable(monkeypatch):
    execution = _execution(
        operation="main_release",
        status="release_planned",
        release_plan={"git_tag": "v1.2.3"},
    )
    monkeypatch.setattr(reconciler.config.release_orchestrator, "enabled_services", ())
    claim = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "claim_publish", claim)

    assert reconciler._publish_if_allowed(execution) is execution
    claim.assert_not_called()


def test_publish_returns_current_state_when_claim_is_already_held(monkeypatch):
    execution = _execution(
        operation="main_release", status="release_planned", canary_approved=True
    )
    current = {**execution, "status": "publish_pending"}
    monkeypatch.setattr(reconciler.release_executions, "claim_publish", lambda _: False)
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: current)

    assert reconciler._publish_if_allowed(execution) is current


def test_publish_claim_without_visible_pending_state_is_noop(monkeypatch):
    execution = _execution(
        operation="main_release", status="release_planned", canary_approved=True
    )
    monkeypatch.setattr(reconciler.release_executions, "claim_publish", lambda _: True)
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: execution)
    publish = mock.Mock()
    monkeypatch.setattr(reconciler.github_release_control, "publish_release", publish)

    assert reconciler._publish_if_allowed(execution) is execution
    publish.assert_not_called()


def test_unknown_nonpublication_failure_is_not_retried(monkeypatch):
    execution = _execution(
        operation="main_release",
        status="unknown",
        canary_approved=True,
        publication_uncertain=False,
    )
    publish = mock.Mock()
    monkeypatch.setattr(reconciler.github_release_control, "publish_release", publish)

    assert reconciler._publish_if_allowed(execution) is execution
    publish.assert_not_called()


def test_publish_conflict_becomes_action_required_unknown(monkeypatch):
    execution = _execution(
        operation="main_release", status="publish_pending", canary_approved=True
    )
    monkeypatch.setattr(
        reconciler.github_release_control,
        "publish_release",
        mock.Mock(
            side_effect=reconciler.github_release_control.GitHubReleaseConflict(
                "tag points elsewhere"
            )
        ),
    )
    save = mock.Mock(
        return_value={**execution, "status": "unknown", "error": "tag points elsewhere"}
    )
    complete = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    monkeypatch.setattr(reconciler, "_complete_check", complete)

    result = reconciler._publish_if_allowed(execution)

    assert result["status"] == "unknown"
    save.assert_called_once_with(
        "execution-1", status="unknown", error="tag points elsewhere"
    )
    complete.assert_called_once_with(
        result, "release", "action_required", "tag points elsewhere"
    )


def test_uncertain_publish_hides_provider_error_and_stops_blind_retry(monkeypatch):
    execution = _execution(
        operation="main_release", status="publish_pending", canary_approved=True
    )
    monkeypatch.setattr(
        reconciler.github_release_control,
        "publish_release",
        mock.Mock(side_effect=TimeoutError("response contained secret")),
    )
    save = mock.Mock(
        return_value={
            **execution,
            "status": "unknown",
            "error": "GitHub release publication is uncertain",
            "publication_uncertain": True,
            "publication_error": "response contained secret",
        }
    )
    complete = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    monkeypatch.setattr(reconciler, "_complete_check", complete)

    result = reconciler._publish_if_allowed(execution)

    assert result["error"] == "GitHub release publication is uncertain"
    assert "secret" not in result["error"]
    save.assert_called_once_with(
        "execution-1",
        status="unknown",
        error="GitHub release publication is uncertain",
        publication_uncertain=True,
        publication_error="response contained secret",
    )
    complete.assert_called_once_with(
        result,
        "release",
        "action_required",
        "response contained secret",
    )


def test_successful_publish_records_release_and_completes_check(monkeypatch):
    execution = _execution(
        operation="main_release", status="publish_pending", canary_approved=True
    )
    published = {
        "tag": "v1.2.3",
        "tag_sha": HEAD,
        "release_id": 123,
        "release_url": "https://github/release/123",
    }
    monkeypatch.setattr(
        reconciler.github_release_control, "publish_release", lambda _: published
    )
    save = mock.Mock(return_value={**execution, **published, "status": "released"})
    complete = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    monkeypatch.setattr(reconciler, "_complete_check", complete)

    result = reconciler._publish_if_allowed(execution)

    assert result["status"] == "released"
    save.assert_called_once_with(
        "execution-1",
        status="released",
        publication_uncertain=False,
        **published,
    )
    complete.assert_called_once_with(result, "release", "success", "Published v1.2.3.")


def test_reconcile_unknown_execution_raises_key_error(monkeypatch):
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: None)
    with pytest.raises(KeyError, match="missing"):
        reconciler.reconcile("missing")


@pytest.mark.parametrize(
    "status", ["quality_failed", "no_release", "released", "failed"]
)
def test_reconcile_terminal_state_is_idempotent(monkeypatch, status):
    execution = _execution(status=status)
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: execution)
    provider = mock.Mock()
    monkeypatch.setattr(reconciler, "_provider_success", provider)

    assert reconciler.reconcile("execution-1") is execution
    provider.assert_not_called()


@pytest.mark.parametrize("status", ["release_planned", "publish_pending"])
def test_reconcile_publication_state_skips_quality_recommit(monkeypatch, status):
    execution = _execution(operation="main_release", status=status)
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: execution)
    publish = mock.Mock(return_value={**execution, "status": "released"})
    commit = mock.Mock()
    monkeypatch.setattr(reconciler, "_publish_if_allowed", publish)
    monkeypatch.setattr(reconciler, "_commit_quality", commit)

    assert reconciler.reconcile("execution-1")["status"] == "released"
    publish.assert_called_once_with(execution)
    commit.assert_not_called()


def test_reconcile_does_not_commit_before_provider_terminal_success(monkeypatch):
    execution = _execution(pending_report_hash="e" * 64)
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(
        reconciler.release_cloud_build,
        "get_build",
        lambda _: _build(status="WORKING"),
    )
    get_pending = mock.Mock()
    immutable = mock.Mock()
    monkeypatch.setattr(reconciler.quality_store, "get_pending_report", get_pending)
    monkeypatch.setattr(reconciler.quality_store, "save_immutable_report", immutable)

    assert reconciler.reconcile("execution-1") is execution
    get_pending.assert_not_called()
    immutable.assert_not_called()


def test_reconcile_uses_fresh_execution_after_provider_check(monkeypatch):
    before = _execution()
    after = {**before, "provider_status": "SUCCESS"}
    reads = iter((before, after))
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: next(reads))
    monkeypatch.setattr(reconciler, "_provider_success", lambda _: True)
    commit = mock.Mock(return_value={**after, "status": "quality_passed"})
    monkeypatch.setattr(reconciler, "_commit_quality", commit)

    result = reconciler.reconcile("execution-1")

    assert result["status"] == "quality_passed"
    commit.assert_called_once_with(after)


def test_reconcile_returns_latest_state_when_provider_is_not_terminal(monkeypatch):
    execution = _execution()
    latest = {**execution, "provider_status": "WORKING"}
    reads = iter((execution, latest))
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: next(reads))
    monkeypatch.setattr(reconciler, "_provider_success", lambda _: False)

    assert reconciler.reconcile("execution-1") is latest


def test_reconcile_submits_reserved_cloud_build_execution(monkeypatch):
    execution = _execution(status="submission_pending", build_id="")
    submitted = {**execution, "status": "running_quality", "build_id": "build-1"}
    reads = iter((execution, submitted))
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: next(reads))
    service = object()
    monkeypatch.setattr(reconciler.catalog, "get_service", lambda _: service)
    submit = mock.Mock(return_value=submitted)
    monkeypatch.setattr(reconciler.release_cloud_build, "submit", submit)
    monkeypatch.setattr(reconciler, "_provider_success", lambda _: False)

    result = reconciler.reconcile("execution-1")

    assert result is submitted
    submit.assert_called_once_with("execution-1", service)


def test_reconcile_preserves_failed_submission_result(monkeypatch):
    execution = _execution(status="submission_pending", build_id="")
    failed = {
        **execution,
        "status": "failed",
        "check_ids": {"quality": 10, "workflows": 11},
    }
    reads = iter((execution, failed))
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: next(reads))
    service = object()
    monkeypatch.setattr(reconciler.catalog, "get_service", lambda _: service)
    monkeypatch.setattr(
        reconciler.release_cloud_build,
        "submit",
        mock.Mock(
            side_effect=reconciler.release_cloud_build.ReleaseCloudBuildError(
                "submit failed"
            )
        ),
    )
    complete = mock.Mock()
    monkeypatch.setattr(reconciler, "_complete_check", complete)

    result = reconciler.reconcile("execution-1")

    assert result is failed
    assert complete.call_args_list == [
        mock.call(
            failed,
            "quality",
            "failure",
            "Release quality could not start. No build was created.",
        ),
        mock.call(
            failed,
            "workflows",
            "failure",
            "Release quality could not start. No build was created.",
        ),
    ]


def test_reconcile_fails_if_reserved_service_disappeared(monkeypatch):
    execution = _execution(status="submission_pending", build_id="")
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(reconciler.catalog, "get_service", lambda _: None)
    submit = mock.Mock()
    monkeypatch.setattr(reconciler.release_cloud_build, "submit", submit)

    with pytest.raises(ValueError, match="Release service disappeared"):
        reconciler.reconcile("execution-1")

    submit.assert_not_called()


def test_reconcile_stops_after_quality_policy_failure(monkeypatch):
    execution = _execution(operation="main_release")
    reads = iter((execution, execution))
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: next(reads))
    monkeypatch.setattr(reconciler, "_provider_success", lambda _: True)
    monkeypatch.setattr(
        reconciler,
        "_commit_quality",
        lambda _: {**execution, "status": "quality_failed"},
    )
    publish = mock.Mock()
    monkeypatch.setattr(reconciler, "_publish_if_allowed", publish)

    result = reconciler.reconcile("execution-1")

    assert result["status"] == "quality_failed"
    publish.assert_not_called()


def test_successful_pr_quality_is_no_longer_due(monkeypatch):
    fingerprint = execution_store.fingerprint(
        repository="owner/eng-platform-web",
        service_name="eng-platform-web",
        operation="pr_quality",
        head_sha=HEAD,
        base_sha=BASE,
        profile_hash="profile",
        executor_digest=EXECUTOR_DIGEST,
        policy_hash="policy",
    )
    execution, _ = execution_store.reserve(
        fingerprint_value=fingerprint,
        repository="owner/eng-platform-web",
        service_name="eng-platform-web",
        operation="pr_quality",
        head_sha=HEAD,
        base_sha=BASE,
        branch="feature/test",
        profile_hash="profile",
        executor_digest=EXECUTOR_DIGEST,
        policy_hash="policy",
        provider="github_actions",
    )
    execution_store.save(
        execution["execution_id"],
        status="running_quality",
        provider_terminal_success=True,
    )
    monkeypatch.setattr(
        reconciler,
        "_commit_quality",
        lambda value: execution_store.save(
            value["execution_id"], status="quality_passed", evidence_committed=True
        ),
    )

    result = reconciler.reconcile(execution["execution_id"])

    assert result["status"] == "quality_passed"
    assert execution["execution_id"] not in {
        item["execution_id"] for item in execution_store.list_due()
    }


@pytest.mark.parametrize(
    "execution",
    [
        _execution(
            operation="main_release",
            engine_event_status="no_release",
            release_plan={},
        ),
        _execution(
            operation="main_release",
            engine_event_status="quality_passed",
            release_plan={"release_type": "none"},
        ),
    ],
)
def test_reconcile_no_release_is_terminal_and_neutral(monkeypatch, execution):
    reads = iter((execution, execution))
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: next(reads))
    monkeypatch.setattr(reconciler, "_provider_success", lambda _: True)
    monkeypatch.setattr(
        reconciler,
        "_commit_quality",
        lambda _: {**execution, "status": "quality_passed"},
    )
    save = mock.Mock(return_value={**execution, "status": "no_release"})
    complete = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    monkeypatch.setattr(reconciler, "_complete_check", complete)

    result = reconciler.reconcile("execution-1")

    assert result["status"] == "no_release"
    save.assert_called_once_with("execution-1", status="no_release")
    complete.assert_called_once_with(
        result, "release", "neutral", "No releasable changes."
    )


def test_reconcile_missing_release_plan_becomes_action_required(monkeypatch):
    execution = _execution(operation="main_release", release_plan={})
    reads = iter((execution, execution))
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: next(reads))
    monkeypatch.setattr(reconciler, "_provider_success", lambda _: True)
    monkeypatch.setattr(
        reconciler,
        "_commit_quality",
        lambda _: {**execution, "status": "quality_passed"},
    )
    save = mock.Mock(return_value={**execution, "status": "unknown"})
    complete = mock.Mock()
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    monkeypatch.setattr(reconciler, "_complete_check", complete)

    result = reconciler.reconcile("execution-1")

    assert result["status"] == "unknown"
    save.assert_called_once_with(
        "execution-1", status="unknown", error="Release plan is missing"
    )
    complete.assert_called_once_with(
        result, "release", "action_required", "Release plan is missing."
    )


def test_reconcile_release_plan_transitions_then_publishes(monkeypatch):
    execution = _execution(
        operation="main_release",
        release_plan={"release_type": "patch", "git_tag": "v1.2.3"},
    )
    reads = iter((execution, execution))
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: next(reads))
    monkeypatch.setattr(reconciler, "_provider_success", lambda _: True)
    monkeypatch.setattr(
        reconciler,
        "_commit_quality",
        lambda _: {**execution, "status": "quality_passed"},
    )
    planned = {**execution, "status": "release_planned"}
    save = mock.Mock(return_value=planned)
    publish = mock.Mock(return_value={**planned, "status": "released"})
    monkeypatch.setattr(reconciler.release_executions, "save", save)
    monkeypatch.setattr(reconciler, "_publish_if_allowed", publish)

    result = reconciler.reconcile("execution-1")

    assert result["status"] == "released"
    save.assert_called_once_with("execution-1", status="release_planned")
    publish.assert_called_once_with(planned)


def test_uncertain_partial_tag_publication_is_reconciled_idempotently(monkeypatch):
    state = _execution(
        operation="main_release",
        status="release_planned",
        canary_approved=True,
        release_plan={
            "release_type": "patch",
            "git_tag": "v1.2.3",
            "next_version": "1.2.3",
        },
    )
    monkeypatch.setattr(reconciler.release_executions, "get", lambda _: dict(state))

    def claim(_):
        state["status"] = "publish_pending"
        state["publish_claimed_at"] = "2026-09-22T12:00:00Z"
        return True

    monkeypatch.setattr(reconciler.release_executions, "claim_publish", claim)
    monkeypatch.setattr(reconciler.release_executions, "save", _save_merged(state))
    publish = mock.Mock(
        side_effect=[
            TimeoutError("response lost after tag creation"),
            {
                "tag": "v1.2.3",
                "tag_sha": HEAD,
                "release_id": 123,
                "release_url": "https://github/release/123",
            },
        ]
    )
    monkeypatch.setattr(reconciler.github_release_control, "publish_release", publish)
    monkeypatch.setattr(reconciler, "_complete_check", mock.Mock())

    first = reconciler.reconcile("execution-1")
    second = reconciler.reconcile("execution-1")

    assert first["status"] == "unknown"
    assert second["status"] == "released"
    assert second["tag"] == "v1.2.3"
    assert publish.call_count == 2
