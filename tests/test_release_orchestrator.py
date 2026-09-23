"""Provider-terminal release failure tests for the GitHub orchestrator."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

from eng_platform_api.models import QualityCheck, QualityReportCreate
from eng_platform_api.services import release_orchestrator as orchestrator


HEAD = "a" * 40
BASE = "b" * 40
REPORT_HASH = "e" * 64
WORKFLOW = ".github/workflows/eng-platform-release.yml"


def _execution(**overrides) -> dict:
    value = {
        "execution_id": "execution-1",
        "fingerprint": "f" * 64,
        "repository": "owner/eng-platform-web",
        "service_name": "eng-platform-web",
        "operation": "main_release",
        "head_sha": HEAD,
        "base_sha": BASE,
        "profile_hash": "profile-hash",
        "policy_hash": "policy-hash",
        "executor_digest": "quality@sha256:" + "d" * 64,
        "provider": "github_actions",
        "status": "running_quality",
        "event_sequence": 2,
        "engine_event_status": "quality_passed",
        "pending_report_hash": REPORT_HASH,
        "check_ids": {"quality": 10, "workflows": 11, "release": 12},
    }
    value.update(overrides)
    return value


def _report() -> QualityReportCreate:
    return QualityReportCreate(
        service_name="eng-platform-web",
        repository="owner/eng-platform-web",
        commit_sha=HEAD,
        base_sha=BASE,
        branch="main",
        profile="node",
        generated_at="2026-09-22T12:00:00+00:00",
        coverage=90.0,
        coverage_threshold=80.0,
        policy_version="oss-v2",
        differential_coverage=90.0,
        differential_threshold=80.0,
        changed_lines=10,
        covered_changed_lines=9,
        checks=[QualityCheck(name="tests", category="tests", status="PASSED")],
    )


def _payload(conclusion: str = "failure") -> dict:
    return {
        "action": "completed",
        "repository": {"full_name": "owner/eng-platform-web"},
        "workflow_run": {
            "id": 42,
            "event": "push",
            "path": WORKFLOW,
            "head_sha": HEAD,
            "html_url": "https://github.example/actions/runs/42",
            "conclusion": conclusion,
        },
    }


def _prepare_github_failure(monkeypatch, state: dict) -> None:
    monkeypatch.setattr(orchestrator, "_close_circuit_from_probe", lambda *_: False)
    monkeypatch.setattr(orchestrator, "_handle_deployment_workflow_run", lambda *_: [])
    monkeypatch.setattr(
        orchestrator.github_release_control,
        "workflow_run",
        lambda *_: SimpleNamespace(
            event="push",
            conclusion="failure",
            head_sha=HEAD,
            path=WORKFLOW,
        ),
    )
    monkeypatch.setattr(
        orchestrator.release_executions,
        "list_for_repository",
        lambda *_: [dict(state)],
    )
    monkeypatch.setattr(orchestrator.release_executions, "get", lambda _: dict(state))
    monkeypatch.setattr(
        orchestrator.release_workflow_identity,
        "matches_workflow_path",
        lambda *_: True,
    )
    monkeypatch.setattr(
        orchestrator.github_actions_quota,
        "is_release_quota_failure",
        lambda *_args, **_kwargs: False,
    )


def test_github_failure_after_quality_commits_evidence_then_fails_release(
    monkeypatch,
):
    state = _execution()
    transitions = []
    _prepare_github_failure(monkeypatch, state)

    def save(execution_id, **changes):
        assert execution_id == state["execution_id"]
        state.update(changes)
        if "status" in changes:
            transitions.append(changes["status"])
        return dict(state)

    monkeypatch.setattr(orchestrator.release_executions, "save", save)
    report = _report()
    monkeypatch.setattr(
        orchestrator.release_reconciler.quality_store,
        "get_pending_report",
        lambda *_: report,
    )

    def save_evidence(*args, **kwargs):
        transitions.append("evidence_committed")
        return SimpleNamespace(
            quality_gate_status="PASSED",
            received_at="2026-09-22T12:00:00+00:00",
        ), REPORT_HASH

    immutable = mock.Mock(side_effect=save_evidence)
    monkeypatch.setattr(
        orchestrator.release_reconciler.quality_store,
        "save_immutable_report",
        immutable,
    )
    summary = mock.Mock()
    monkeypatch.setattr(
        orchestrator.release_reconciler.quality_store,
        "save_execution_summary",
        summary,
    )
    monkeypatch.setattr(
        orchestrator.release_reconciler.catalog,
        "get_service",
        lambda _: object(),
    )
    monkeypatch.setattr(orchestrator.release_reconciler, "policy_errors", lambda *_: [])
    complete = mock.Mock()
    monkeypatch.setattr(orchestrator.release_reconciler, "_complete_check", complete)
    publish = mock.Mock()
    monkeypatch.setattr(orchestrator.release_reconciler, "_publish_if_allowed", publish)

    results = orchestrator.handle_workflow_run(_payload(), delivery_id="delivery-1")

    assert results == [state]
    assert state["status"] == "failed"
    assert state["provider_terminal_success"] is True
    assert state["evidence_committed"] is True
    assert state["report_hash"] == REPORT_HASH
    assert state["release_engine_failed"] is True
    assert state["error"] == "Release workflow failed after quality passed"
    assert transitions == ["evidence_committed", "quality_passed", "failed"]
    immutable.assert_called_once()
    summary.assert_called_once()
    assert summary.call_args.args[0]["operation"] == "main_release"
    assert summary.call_args.args[2] == REPORT_HASH
    publish.assert_not_called()
    assert complete.call_args_list[-1] == mock.call(
        state,
        "release",
        "failure",
        "Release workflow failed after quality passed",
    )


def test_github_failure_before_quality_is_terminal_without_evidence(monkeypatch):
    state = _execution(
        event_sequence=1,
        engine_event_status="running_quality",
        pending_report_hash="",
    )
    _prepare_github_failure(monkeypatch, state)

    def save(execution_id, **changes):
        assert execution_id == state["execution_id"]
        state.update(changes)
        return dict(state)

    monkeypatch.setattr(orchestrator.release_executions, "save", save)
    reconcile = mock.Mock()
    monkeypatch.setattr(orchestrator.release_reconciler, "reconcile", reconcile)
    immutable = mock.Mock()
    monkeypatch.setattr(
        orchestrator.release_reconciler.quality_store,
        "save_immutable_report",
        immutable,
    )

    results = orchestrator.handle_workflow_run(_payload(), delivery_id="delivery-1")

    assert results == [state]
    assert state["status"] == "failed"
    assert state["error"] == "GitHub workflow concluded failure"
    assert not state.get("evidence_committed")
    assert not state.get("release_engine_failed")
    immutable.assert_not_called()
    reconcile.assert_not_called()
