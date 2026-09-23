"""Provider selection, webhook idempotence, and administrator probe paths."""

from types import SimpleNamespace
from unittest import mock

import pytest

from eng_platform_api.services import release_orchestrator as orchestrator


HEAD = "a" * 40
BASE = "b" * 40
REPOSITORY = "owner/private-repo"


def _service():
    return SimpleNamespace(repository=REPOSITORY, service_name="example-service")


def _execution(**changes):
    execution = {
        "execution_id": "current",
        "repository": REPOSITORY,
        "head_sha": HEAD,
        "base_sha": BASE,
        "operation": "pr_quality",
        "provider": "github_actions",
        "status": "reserved",
        "check_ids": {},
    }
    execution.update(changes)
    return execution


def test_provider_keeps_public_repositories_on_github_and_fail_opens_billing(
    monkeypatch,
):
    service = _service()
    private = mock.Mock(return_value=False)
    usage = mock.Mock()
    monkeypatch.setattr(
        orchestrator.github_release_control, "repository_is_private", private
    )
    monkeypatch.setattr(orchestrator.github_actions_quota, "current_usage", usage)
    assert orchestrator._provider(service) == "github_actions"
    usage.assert_not_called()

    private.side_effect = RuntimeError("temporary GitHub API failure")
    assert orchestrator._provider(service) == "github_actions"
    usage.assert_not_called()


def test_provider_opens_persistent_circuit_only_for_confirmed_quota(monkeypatch):
    service = _service()
    opened = mock.Mock()
    monkeypatch.setattr(
        orchestrator.github_release_control, "repository_is_private", lambda _: True
    )
    monkeypatch.setattr(orchestrator.executor_circuits, "is_open", lambda _: False)
    monkeypatch.setattr(orchestrator, "_open_circuit", opened)
    monkeypatch.setattr(
        orchestrator.github_actions_quota, "current_usage", lambda: None
    )
    assert orchestrator._provider(service) == "github_actions"
    opened.assert_not_called()

    monkeypatch.setattr(
        orchestrator.github_actions_quota,
        "current_usage",
        lambda: SimpleNamespace(exhausted=True, private_linux_minutes=2001),
    )
    assert orchestrator._provider(service) == "cloud_build"
    assert opened.call_args.kwargs["reason"] == "included_private_minutes_exhausted"

    opened.reset_mock()
    monkeypatch.setattr(orchestrator.executor_circuits, "is_open", lambda _: True)
    assert orchestrator._provider(service) == "cloud_build"
    assert (
        opened.call_args.kwargs["reason"] == "persistent_github_actions_billing_circuit"
    )


def test_open_circuit_persists_then_propagates_to_private_repositories(monkeypatch):
    persisted = mock.Mock()
    set_mode = mock.Mock()
    monkeypatch.setattr(orchestrator.executor_circuits, "open_circuit", persisted)
    monkeypatch.setattr(
        orchestrator.catalog,
        "get_services",
        lambda: SimpleNamespace(
            services=[_service(), SimpleNamespace(repository="owner/public")]
        ),
    )
    monkeypatch.setattr(
        orchestrator.github_release_control,
        "repository_is_private",
        lambda repository: repository == REPOSITORY,
    )
    monkeypatch.setattr(
        orchestrator.github_release_control, "set_repository_execution_mode", set_mode
    )

    orchestrator._open_circuit(
        repository=REPOSITORY, run_id="42", reason="billing", evidence="quota denied"
    )

    assert persisted.call_args.kwargs["run_id"] == "42"
    set_mode.assert_called_once_with(REPOSITORY, "cloud_build")


def test_explicit_build_rejection_finishes_canonical_checks(monkeypatch):
    execution = {
        "execution_id": "execution-1",
        "provider": "cloud_build",
        "repository": REPOSITORY,
        "head_sha": HEAD,
        "check_ids": {"quality": 41, "workflows": 42, "title": 43},
    }
    monkeypatch.setattr(
        orchestrator.release_cloud_build,
        "submit",
        lambda *_: (_ for _ in ()).throw(
            orchestrator.release_cloud_build.ReleaseCloudBuildError("rejected")
        ),
    )
    monkeypatch.setattr(
        orchestrator.release_executions,
        "get",
        lambda _: {**execution, "status": "failed"},
    )
    upsert = mock.Mock()
    monkeypatch.setattr(orchestrator.github_release_control, "upsert_check", upsert)

    result = orchestrator._submit_if_managed(execution, _service())

    assert result["status"] == "failed"
    assert [call.kwargs["kind"] for call in upsert.call_args_list] == [
        "quality",
        "workflows",
    ]
    assert all(call.kwargs["conclusion"] == "failure" for call in upsert.call_args_list)


def test_start_checks_uses_stable_external_ids_and_conventional_title(monkeypatch):
    checks = mock.Mock(side_effect=[11, 12, 13, 14, 15, 16, 17, 18, 19])
    save = mock.Mock()
    monkeypatch.setattr(orchestrator.github_release_control, "upsert_check", checks)
    monkeypatch.setattr(orchestrator.release_executions, "save", save)

    orchestrator._start_checks(_execution(), pr_title="fix(api): repair fallback")
    first = save.call_args.kwargs["check_ids"]
    assert first == {"quality": 11, "workflows": 12, "title": 13}
    assert checks.call_args.kwargs["conclusion"] == "success"
    assert checks.call_args.kwargs["external_id"] == "current:title"

    orchestrator._start_checks(_execution(), pr_title="invalid title")
    assert checks.call_args.kwargs["conclusion"] == "failure"
    orchestrator._start_checks(_execution(operation="main_release"))
    assert save.call_args.kwargs["check_ids"]["release"] == 19


def test_pr_redelivery_resumes_and_cancels_only_superseded_cloud_build(monkeypatch):
    service = _service()
    current = _execution()
    previous = _execution(
        execution_id="obsolete", provider="cloud_build", pull_request_number=7
    )
    ignored = _execution(
        execution_id="other", provider="github_actions", pull_request_number=7
    )
    save = mock.Mock()
    cancel = mock.Mock()
    start = mock.Mock()
    monkeypatch.setattr(orchestrator, "_services", lambda _: [service])
    monkeypatch.setattr(orchestrator, "_provider", lambda _: "github_actions")
    monkeypatch.setattr(orchestrator, "_reserve", lambda **_: (current, True))
    monkeypatch.setattr(orchestrator.release_executions, "save", save)
    monkeypatch.setattr(orchestrator.release_executions, "get", lambda _: current)
    monkeypatch.setattr(
        orchestrator.release_executions,
        "list_for_repository",
        lambda _: [current, previous, ignored],
    )
    monkeypatch.setattr(orchestrator.release_cloud_build, "cancel", cancel)
    monkeypatch.setattr(orchestrator, "_start_checks", start)
    monkeypatch.setattr(
        orchestrator, "_submit_if_managed", lambda execution, _: execution
    )
    payload = {
        "action": "synchronize",
        "number": 7,
        "repository": {"full_name": REPOSITORY},
        "pull_request": {
            "head": {"sha": HEAD, "ref": "fix/quality"},
            "base": {"sha": BASE},
            "title": "fix: quality",
        },
    }

    assert orchestrator.handle_pull_request(payload, delivery_id="delivery") == [
        current
    ]
    save.assert_called_once_with("current", pull_request_number=7)
    cancel.assert_called_once_with("obsolete")
    start.assert_called_once_with(current, pr_title="fix: quality")

    payload["action"] = "closed"
    assert orchestrator.handle_pull_request(payload, delivery_id="delivery") == []
    payload["action"] = "opened"
    payload["pull_request"]["base"]["sha"] = HEAD
    with pytest.raises(orchestrator.ReleaseOrchestratorError, match="SHAs are invalid"):
        orchestrator.handle_pull_request(payload, delivery_id="delivery")


def test_main_push_requires_fast_forward_and_reuses_reserved_execution(monkeypatch):
    service = _service()
    state = _execution(operation="main_release")
    reserve = mock.Mock(return_value=(state, False))
    checks = mock.Mock()
    monkeypatch.setattr(orchestrator, "_services", lambda _: [service])
    monkeypatch.setattr(orchestrator, "_provider", lambda _: "github_actions")
    monkeypatch.setattr(orchestrator, "_reserve", reserve)
    monkeypatch.setattr(orchestrator, "_start_checks", checks)
    monkeypatch.setattr(orchestrator.release_executions, "get", lambda _: state)
    monkeypatch.setattr(
        orchestrator, "_submit_if_managed", lambda execution, _: execution
    )
    fast_forward = mock.Mock(return_value=True)
    monkeypatch.setattr(
        orchestrator.github_release_control, "is_fast_forward", fast_forward
    )
    payload = {
        "ref": "refs/heads/main",
        "after": HEAD,
        "before": BASE,
        "repository": {"full_name": REPOSITORY},
    }

    assert orchestrator.handle_push(payload, delivery_id="delivery") == [state]
    assert reserve.call_args.kwargs["operation"] == "main_release"
    checks.assert_called_once_with(state)
    fast_forward.assert_called_once_with(REPOSITORY, BASE, HEAD)

    fast_forward.return_value = False
    with pytest.raises(orchestrator.ReleaseOrchestratorError, match="Non-fast-forward"):
        orchestrator.handle_push(payload, delivery_id="delivery")
    payload["before"] = "0" * 40
    with pytest.raises(
        orchestrator.ReleaseOrchestratorError, match="differential base"
    ):
        orchestrator.handle_push(payload, delivery_id="delivery")
    payload["deleted"] = True
    assert orchestrator.handle_push(payload, delivery_id="delivery") == []


def test_health_probe_closes_circuit_only_after_real_started_success(monkeypatch):
    workflow = orchestrator.config.release_orchestrator.github_health_workflow
    circuit = {
        "state": "open",
        "probe": {
            "status": "requested",
            "repository": REPOSITORY,
            "workflow": workflow,
            "nonce": "bound-nonce",
        },
    }
    recorded = mock.Mock()
    closed = mock.Mock()
    set_mode = mock.Mock()
    run = SimpleNamespace(
        conclusion="success",
        jobs=lambda: [SimpleNamespace(started_at="2026-09-23T00:00:00Z")],
    )
    monkeypatch.setattr(orchestrator.executor_circuits, "get", lambda _: circuit)
    monkeypatch.setattr(orchestrator.executor_circuits, "record_probe", recorded)
    monkeypatch.setattr(
        orchestrator.executor_circuits, "close_after_successful_probe", closed
    )
    monkeypatch.setattr(
        orchestrator.github_release_control, "workflow_run", lambda *args: run
    )
    monkeypatch.setattr(
        orchestrator.github_release_control, "set_repository_execution_mode", set_mode
    )
    monkeypatch.setattr(
        orchestrator.catalog,
        "get_services",
        lambda: SimpleNamespace(
            services=[_service(), SimpleNamespace(repository="owner/other")]
        ),
    )
    payload = {
        "path": f"owner/repo/{workflow}",
        "event": "workflow_dispatch",
        "display_title": "eng-platform-health-bound-nonce",
        "id": 42,
    }

    assert orchestrator._close_circuit_from_probe(REPOSITORY, payload) is True
    assert recorded.call_args.kwargs["jobs_started"] == 1
    assert set_mode.call_count == 2
    closed.assert_called_once_with("owner", run_id="42")

    closed.reset_mock()
    run.jobs = lambda: [SimpleNamespace(started_at=None)]
    assert orchestrator._close_circuit_from_probe(REPOSITORY, payload) is True
    closed.assert_not_called()
    assert recorded.call_args.kwargs["jobs_started"] == 0


def test_unbound_health_probe_cannot_close_circuit(monkeypatch):
    workflow = orchestrator.config.release_orchestrator.github_health_workflow
    monkeypatch.setattr(
        orchestrator.executor_circuits,
        "get",
        lambda _: {
            "state": "open",
            "probe": {
                "status": "requested",
                "repository": REPOSITORY,
                "workflow": workflow,
                "nonce": "expected",
            },
        },
    )
    run = mock.Mock()
    monkeypatch.setattr(orchestrator.github_release_control, "workflow_run", run)
    assert (
        orchestrator._close_circuit_from_probe(REPOSITORY, {"path": "other.yml"})
        is False
    )
    assert (
        orchestrator._close_circuit_from_probe(
            REPOSITORY,
            {
                "path": workflow,
                "event": "workflow_dispatch",
                "display_title": "eng-platform-health-forged",
            },
        )
        is True
    )
    run.assert_not_called()


def test_probe_variable_update_failure_preserves_open_circuit(monkeypatch):
    workflow = orchestrator.config.release_orchestrator.github_health_workflow
    monkeypatch.setattr(
        orchestrator.executor_circuits,
        "get",
        lambda _: {
            "state": "open",
            "probe": {
                "status": "requested",
                "repository": REPOSITORY,
                "workflow": workflow,
                "nonce": "expected",
            },
        },
    )
    monkeypatch.setattr(
        orchestrator.github_release_control,
        "workflow_run",
        lambda *args: SimpleNamespace(
            conclusion="success", jobs=lambda: [SimpleNamespace(started_at="now")]
        ),
    )
    monkeypatch.setattr(
        orchestrator.executor_circuits, "record_probe", lambda *args, **kwargs: None
    )
    closed = mock.Mock()
    monkeypatch.setattr(
        orchestrator.executor_circuits, "close_after_successful_probe", closed
    )
    monkeypatch.setattr(
        orchestrator.catalog,
        "get_services",
        lambda: SimpleNamespace(
            services=[_service(), SimpleNamespace(repository="owner/second")]
        ),
    )
    calls = []

    def set_mode(repository, mode):
        calls.append((repository, mode))
        if (
            mode == "github_actions"
            and sum(recorded_mode == "github_actions" for _, recorded_mode in calls)
            == 2
        ):
            raise RuntimeError("GitHub API rejected update")

    monkeypatch.setattr(
        orchestrator.github_release_control, "set_repository_execution_mode", set_mode
    )
    payload = {
        "path": workflow,
        "event": "workflow_dispatch",
        "display_title": "eng-platform-health-expected",
        "id": 43,
    }
    assert orchestrator._close_circuit_from_probe(REPOSITORY, payload) is True
    assert any(mode == "cloud_build" for _, mode in calls)
    closed.assert_not_called()
