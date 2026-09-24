"""Admin recovery for the legacy semantic-release Billing cutover."""

from __future__ import annotations

from types import SimpleNamespace
from unittest import mock

import pytest

from eng_platform_api.services import release_orchestrator as orchestrator


HEAD = "a" * 40
BASE = "b" * 40
REPOSITORY = "diegomad14/gcp-engineering-platform-web"


def _paired_runs(*, paired_sha: str = HEAD, paired_branch: str = "main"):
    billing_run = SimpleNamespace(
        head_sha=HEAD,
        head_branch="main",
        event="push",
        status="completed",
        conclusion="failure",
        path=".github/workflows/semantic-release.yml@refs/heads/main",
        html_url="https://github.com/diegomad14/gcp-engineering-platform-web/actions/runs/1",
    )
    platform_run = SimpleNamespace(
        head_sha=paired_sha,
        head_branch=paired_branch,
        event="push",
        status="completed",
        conclusion="skipped",
        path=".github/workflows/eng-platform-release.yml@refs/heads/main",
    )
    return billing_run, platform_run


def _configure(
    monkeypatch,
    *,
    paired_sha: str = HEAD,
    paired_branch: str = "main",
    mode: str = "cloud_build",
):
    service = SimpleNamespace(service_name="eng-platform-web", repository=REPOSITORY)
    billing_run, platform_run = _paired_runs(
        paired_sha=paired_sha, paired_branch=paired_branch
    )
    monkeypatch.setattr(orchestrator.catalog, "get_service", lambda _: service)
    monkeypatch.setattr(orchestrator, "_service_enabled", lambda _: True)
    monkeypatch.setattr(
        orchestrator.github_release_control, "is_fast_forward", lambda *_: True
    )
    monkeypatch.setattr(
        orchestrator.github_release_control,
        "workflow_run",
        lambda _repository, run_id: {1: billing_run, 2: platform_run}[run_id],
    )
    execution_mode = mock.Mock(return_value=mode)
    monkeypatch.setattr(
        orchestrator.github_release_control,
        "repository_execution_mode",
        execution_mode,
    )
    monkeypatch.setattr(
        orchestrator.github_release_control,
        "current_default_sha",
        lambda _: HEAD,
    )
    monkeypatch.setattr(
        orchestrator.github_actions_quota,
        "is_release_quota_failure",
        lambda run, **_: run is billing_run,
    )
    execution = {
        "execution_id": "execution-web-main",
        "repository": REPOSITORY,
        "service_name": "eng-platform-web",
        "operation": "main_release",
        "head_sha": HEAD,
        "base_sha": BASE,
    }
    monkeypatch.setattr(orchestrator, "_reserve", lambda **_: (execution, True))
    monkeypatch.setattr(orchestrator, "_start_checks", mock.Mock())
    monkeypatch.setattr(
        orchestrator.release_executions,
        "save",
        lambda _execution_id, **changes: {**execution, **changes},
    )
    monkeypatch.setattr(orchestrator, "_open_circuit", mock.Mock())
    monkeypatch.setattr(
        orchestrator.release_executions,
        "transition_to_cloud_build",
        lambda *_args, **_kwargs: ({**execution, "provider": "cloud_build"}, True),
    )
    submit = mock.Mock(return_value={**execution, "provider": "cloud_build"})
    monkeypatch.setattr(orchestrator, "_submit_if_managed", submit)
    return execution, submit, execution_mode


def test_legacy_semantic_release_billing_requires_paired_skipped_platform_run(
    monkeypatch,
):
    execution, submit, execution_mode = _configure(monkeypatch)

    result = orchestrator.reconcile_verified_github_run(
        service_name="eng-platform-web",
        operation="main_release",
        head_sha=HEAD,
        base_sha=BASE,
        run_id=1,
        paired_run_id=2,
        requested_by="diegomad14",
    )

    assert result["execution_id"] == execution["execution_id"]
    submit.assert_called_once()
    execution_mode.assert_called_once_with(REPOSITORY)


@pytest.mark.parametrize(
    ("paired_sha", "paired_branch", "mode"),
    [
        ("c" * 40, "main", "cloud_build"),
        (HEAD, "feature", "cloud_build"),
        (HEAD, "main", "github_actions"),
    ],
)
def test_legacy_billing_recovery_fails_closed_for_wrong_pair_or_mode(
    monkeypatch, paired_sha, paired_branch, mode
):
    _, submit, _ = _configure(
        monkeypatch,
        paired_sha=paired_sha,
        paired_branch=paired_branch,
        mode=mode,
    )

    with pytest.raises(orchestrator.ReleaseOrchestratorError):
        orchestrator.reconcile_verified_github_run(
            service_name="eng-platform-web",
            operation="main_release",
            head_sha=HEAD,
            base_sha=BASE,
            run_id=1,
            paired_run_id=2,
            requested_by="diegomad14",
        )

    submit.assert_not_called()


def test_legacy_billing_recovery_requires_paired_run_id(monkeypatch):
    _, submit, _ = _configure(monkeypatch)

    with pytest.raises(orchestrator.ReleaseOrchestratorError):
        orchestrator.reconcile_verified_github_run(
            service_name="eng-platform-web",
            operation="main_release",
            head_sha=HEAD,
            base_sha=BASE,
            run_id=1,
            requested_by="diegomad14",
        )

    submit.assert_not_called()
