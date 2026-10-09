"""Automatic fallback requires positive, pre-start GitHub billing evidence."""

from types import SimpleNamespace
from unittest import mock

import pytest

from eng_platform_api.models import DeploymentItem, ReleaseTag
from eng_platform_api.services import deployment_commands as commands
from eng_platform_api.services import github_actions_quota as quota
from eng_platform_api.services import github_deployments
from eng_platform_api.services import release_orchestrator as orchestrator


@pytest.mark.parametrize(
    "message",
    [
        "billing service unavailable",
        "Resource not accessible by integration: missing billing permission",
        "API rate limit exceeded",
        "Artifact storage quota exceeded",
        "Cloud Build quota exceeded",
        "spending limit lookup failed",
        "Included minutes lookup failed",
        "billing tests failed with exit code 1",
        "Workflow YAML validation failed",
        "Network timeout during billing lookup",
        "Insufficient Actions permissions",
    ],
)
def test_unrelated_failures_do_not_trigger_quota_fallback(message):
    assert quota.is_quota_error(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "The job was not started because recent account payments have failed "
        "or your spending limit needs to be increased.",
        "Included minutes quota exceeded",
        "Included minutes have been exhausted",
        "Actions minute quota exceeded",
        "GitHub Actions usage quota exhausted",
        "Spending limit has been reached",
        "Payment required",
    ],
)
def test_explicit_rejections_are_recognized(message):
    assert quota.is_quota_error(message) is True


@pytest.mark.parametrize("conclusion", ["success", "cancelled", "timed_out", "skipped"])
def test_only_failed_runs_can_be_billing_rejections(monkeypatch, conclusion):
    monkeypatch.setattr(quota.config.cloud_build, "enabled", True)
    monkeypatch.setattr(quota.config.cloud_build, "mode", "auto")
    monkeypatch.setattr(quota.config.cloud_build, "enabled_services", ("service",))
    annotation = mock.Mock(return_value=True)
    monkeypatch.setattr(quota, "_job_annotations_are_quota_failure", annotation)
    item = SimpleNamespace(service_name="service")
    run = SimpleNamespace(conclusion=conclusion)
    assert quota.is_reactive_quota_failure(run, item) is False
    assert (
        quota.is_release_quota_failure(
            run, repository="owner/repo", expected_sha="a" * 40
        )
        is False
    )
    annotation.assert_not_called()


@pytest.fixture(autouse=True)
def automatic_executor_policy(monkeypatch):
    """These regression cases exercise the retained billing-only auto mode."""
    monkeypatch.setattr(
        orchestrator.config.release_orchestrator, "private_executor_mode", "auto"
    )


def _annotation_run(monkeypatch, *, steps, message):
    requester = mock.Mock()
    requester.requestJsonAndCheck.return_value = (None, [{"message": message}])
    client = mock.Mock()
    client.get_repo.return_value._requester = requester
    monkeypatch.setattr(quota, "github_client", lambda: client)
    job = SimpleNamespace(id=42, steps=steps)
    run = SimpleNamespace(
        conclusion="failure", event="push", head_sha="a" * 40, jobs=lambda: [job]
    )
    return run, requester


@pytest.mark.parametrize(
    "steps", [[], [SimpleNamespace(name="tests", conclusion="failure")]]
)
@pytest.mark.parametrize(
    "message", ["spending limit needs to be increased", "billing service unavailable"]
)
def test_release_billing_requires_explicit_annotation_and_no_steps(
    monkeypatch, steps, message
):
    run, requester = _annotation_run(monkeypatch, steps=steps, message=message)
    assert quota.is_release_quota_failure(
        run, repository="owner/repo", expected_sha="a" * 40
    ) is (not steps and message.startswith("spending limit"))
    if steps:
        requester.requestJsonAndCheck.assert_not_called()
    run.head_sha = "b" * 40
    assert (
        quota.is_release_quota_failure(
            run, repository="owner/repo", expected_sha="a" * 40
        )
        is False
    )


def test_no_job_release_startup_failure_does_not_use_usage_to_infer_rejection(
    monkeypatch,
):
    usage = mock.Mock(return_value=SimpleNamespace(exhausted=True))
    monkeypatch.setattr(quota, "current_usage", usage)
    run = SimpleNamespace(
        conclusion="startup_failure", event="push", head_sha="a" * 40, jobs=lambda: []
    )
    assert (
        quota.is_release_quota_failure(
            run, repository="owner/repo", expected_sha="a" * 40
        )
        is False
    )
    usage.assert_not_called()


@pytest.mark.parametrize("operation", ["deploy", "rollback", "retry"])
@pytest.mark.parametrize("policy", ["disabled", "unenrolled", "github_actions"])
def test_direct_billing_fallback_respects_service_policy(
    monkeypatch, operation, policy
):
    service = commands.catalog.get_service("eng-platform-api")
    monkeypatch.setattr(quota.config.cloud_build, "enabled", policy != "disabled")
    monkeypatch.setattr(
        quota.config.cloud_build,
        "enabled_services",
        () if policy == "unenrolled" else (service.service_name,),
    )
    monkeypatch.setattr(
        quota.config.cloud_build,
        "mode",
        "github_actions" if policy == "github_actions" else "auto",
    )
    monkeypatch.setattr(quota, "should_use_cloud_build", lambda *_: False)
    item = DeploymentItem(
        id="42",
        service_name=service.service_name,
        repository=service.repository,
        tag="v1.0.0",
        sha="a" * 40,
    )
    error = github_deployments.GitHubDispatchError(
        item, dispatch_error=RuntimeError("Payment required")
    )
    monkeypatch.setattr(
        github_deployments, "start_deployment", mock.Mock(side_effect=error)
    )
    monkeypatch.setattr(
        github_deployments, "start_rollback", mock.Mock(side_effect=error)
    )
    monkeypatch.setattr(
        github_deployments, "retry_dispatch", mock.Mock(side_effect=error)
    )
    monkeypatch.setattr(commands, "_require_release_quality", mock.Mock())
    monkeypatch.setattr(commands, "_require_orchestrated_release", mock.Mock())
    monkeypatch.setattr(commands.deployment_store, "save", mock.Mock())
    fallback = mock.Mock()
    opened = mock.Mock()
    monkeypatch.setattr(commands, "start_cloud_build", fallback)
    monkeypatch.setattr(commands, "_open_billing_circuit", opened)
    with pytest.raises(
        (github_deployments.GitHubDispatchError, commands.HTTPException)
    ):
        if operation == "deploy":
            commands._dispatch_deploy(
                service, ReleaseTag(name=item.tag, sha=item.sha), "operator"
            )
        elif operation == "rollback":
            commands._dispatch_rollback(service, item, "operator")
        else:
            commands._retry_failed_dispatch(service, item, "key", "dispatch failed")
    fallback.assert_not_called()
    opened.assert_not_called()


def test_bound_health_probe_accepts_known_repository_rename_without_rewriting_request(
    monkeypatch,
):
    old = "diegomad14/cgm-sanplat-web"
    renamed = "diegomad14/cgm-artemis-web"
    workflow = orchestrator.config.release_orchestrator.github_health_workflow
    circuits = orchestrator.executor_circuits
    circuits.open_circuit("diegomad14", reason="github_actions_billing_rejection")
    requested = circuits.request_probe(
        "diegomad14", repository=old, workflow=workflow, requested_by="operator"
    )
    nonce = requested["probe"]["nonce"]
    run = SimpleNamespace(
        conclusion="success", jobs=lambda: [SimpleNamespace(started_at="now")]
    )
    fetch = mock.Mock(return_value=run)
    monkeypatch.setattr(orchestrator.github_release_control, "workflow_run", fetch)
    monkeypatch.setattr(
        orchestrator.github_release_control,
        "set_repository_execution_mode",
        mock.Mock(),
    )
    monkeypatch.setattr(
        orchestrator.catalog, "get_services", lambda: SimpleNamespace(services=[])
    )
    payload = {
        "path": workflow,
        "event": "workflow_dispatch",
        "display_title": f"eng-platform-health-{nonce}",
        "id": 42,
    }
    assert orchestrator._close_circuit_from_probe("diegomad14/unrelated", payload)
    fetch.assert_not_called()
    assert circuits.is_open("diegomad14")
    assert orchestrator._close_circuit_from_probe(renamed, payload)
    fetch.assert_called_once_with(renamed, 42)
    closed = circuits.get("diegomad14")
    assert closed["state"] == "closed"
    assert closed["probe"]["repository"] == old
    assert closed["probe"]["nonce"] == nonce


@pytest.mark.parametrize("operation", ["deploy", "rollback", "retry"])
def test_enrolled_direct_billing_rejection_falls_back_once_with_same_identity(
    monkeypatch, operation
):
    service = commands.catalog.get_service("eng-platform-api")
    monkeypatch.setattr(quota.config.cloud_build, "enabled", True)
    monkeypatch.setattr(quota.config.cloud_build, "mode", "auto")
    monkeypatch.setattr(
        quota.config.cloud_build, "enabled_services", (service.service_name,)
    )
    monkeypatch.setattr(quota, "should_use_cloud_build", lambda *_: False)
    item = DeploymentItem(
        id="42",
        service_name=service.service_name,
        repository=service.repository,
        tag="v1.0.0",
        sha="a" * 40,
        kind="rollback" if operation == "rollback" else "deploy",
    )
    error = github_deployments.GitHubDispatchError(
        item, dispatch_error=RuntimeError("Payment required")
    )
    for method in ("start_deployment", "start_rollback", "retry_dispatch"):
        monkeypatch.setattr(github_deployments, method, mock.Mock(side_effect=error))
    monkeypatch.setattr(commands, "_require_release_quality", mock.Mock())
    monkeypatch.setattr(commands, "_require_orchestrated_release", mock.Mock())
    save = mock.Mock(side_effect=lambda value, _key: value)
    monkeypatch.setattr(commands.deployment_store, "save", save)
    fallback = mock.Mock(return_value=item)
    opened = mock.Mock()
    monkeypatch.setattr(commands, "start_cloud_build", fallback)
    monkeypatch.setattr(commands, "_open_billing_circuit", opened)
    if operation == "deploy":
        result = commands._dispatch_deploy(
            service, ReleaseTag(name=item.tag, sha=item.sha), "operator"
        )
    elif operation == "rollback":
        result = commands._dispatch_rollback(service, item, "operator")
    else:
        result = commands._retry_failed_dispatch(
            service, item, "original-key", "dispatch failed"
        )
        save.assert_called_once_with(item, "original-key")
    assert result is item
    fallback.assert_called_once_with(
        service,
        item,
        reason="retry_billing" if operation == "retry" else "github_quota_dispatch",
    )
    opened.assert_called_once_with(
        service,
        reason="github_actions_billing_rejection",
        evidence="Payment required",
    )


@pytest.mark.parametrize(
    "reason",
    [
        "github_dispatch_without_run",
        "included_private_minutes_exhausted",
        "unknown",
        "",
    ],
)
def test_legacy_open_circuit_selects_neither_executor_and_preserves_state(
    monkeypatch, reason
):
    service = commands.catalog.get_service("eng-platform-api")
    circuits = orchestrator.executor_circuits
    monkeypatch.setattr(quota.config.github, "billing_owner", "owner")
    monkeypatch.setattr(quota.config.cloud_build, "enabled", True)
    monkeypatch.setattr(quota.config.cloud_build, "mode", "auto")
    monkeypatch.setattr(
        quota.config.cloud_build, "enabled_services", (service.service_name,)
    )
    monkeypatch.setattr(quota.config.cloud_build, "cloud_build_only_services", ())
    monkeypatch.setattr(
        quota,
        "github_client",
        lambda: mock.Mock(get_repo=lambda _: SimpleNamespace(private=True)),
    )
    monkeypatch.setattr(
        orchestrator.github_release_control, "repository_is_private", lambda _: True
    )
    before, _ = circuits.open_circuit("owner", reason=reason)
    propagate = mock.Mock()
    monkeypatch.setattr(orchestrator, "_propagate_open_circuit", propagate)
    with pytest.raises(
        circuits.CircuitRecoveryRequired, match="verified health recovery"
    ):
        quota.should_use_cloud_build(service.service_name, service.repository)
    with pytest.raises(
        orchestrator.ReleaseOrchestratorError, match="verified health recovery"
    ):
        orchestrator._provider(service)
    assert circuits.get("owner") == before
    propagate.assert_not_called()


def test_verified_billing_circuit_still_allows_automatic_fallback(monkeypatch):
    service = commands.catalog.get_service("eng-platform-api")
    circuits = orchestrator.executor_circuits
    monkeypatch.setattr(quota.config.github, "billing_owner", "owner")
    monkeypatch.setattr(quota.config.cloud_build, "enabled", True)
    monkeypatch.setattr(quota.config.cloud_build, "mode", "auto")
    monkeypatch.setattr(
        quota.config.cloud_build, "enabled_services", (service.service_name,)
    )
    monkeypatch.setattr(quota.config.cloud_build, "cloud_build_only_services", ())
    monkeypatch.setattr(
        quota,
        "github_client",
        lambda: mock.Mock(get_repo=lambda _: SimpleNamespace(private=True)),
    )
    monkeypatch.setattr(
        orchestrator.github_release_control, "repository_is_private", lambda _: True
    )
    monkeypatch.setattr(orchestrator, "_propagate_open_circuit", mock.Mock())
    circuits.open_circuit("owner", reason="github_actions_billing_rejection")
    assert quota.should_use_cloud_build(service.service_name, service.repository)
    assert orchestrator._provider(service) == "cloud_build"


@pytest.mark.parametrize("operation", ["deploy", "rollback", "retry"])
def test_recovery_required_surfaces_as_conflict_without_dispatch(
    monkeypatch, operation
):
    service = commands.catalog.get_service("eng-platform-api")
    target = DeploymentItem(
        id="42",
        service_name=service.service_name,
        repository=service.repository,
        tag="v1.0.0",
        sha="a" * 40,
        status="SUCCEEDED",
        production_revision="revision-1",
    )
    monkeypatch.setattr(commands, "_service_or_404", lambda _: service)
    monkeypatch.setattr(commands, "_active_deployment", lambda _: None)
    monkeypatch.setattr(commands, "_require_release_quality", mock.Mock())
    monkeypatch.setattr(commands, "_require_orchestrated_release", mock.Mock())
    monkeypatch.setattr(commands.deployment_store, "get", lambda _: target)
    monkeypatch.setattr(
        commands.deployment_store, "find_by_idempotency_key", lambda _: None
    )
    monkeypatch.setattr(
        github_deployments,
        "get_tag",
        lambda *_: ReleaseTag(name=target.tag, sha=target.sha, eligible=True),
    )
    monkeypatch.setattr(
        quota,
        "should_use_cloud_build",
        mock.Mock(
            side_effect=commands.executor_circuits.CircuitRecoveryRequired(
                "verified health recovery required"
            )
        ),
    )
    github = mock.Mock()
    cloud = mock.Mock()
    for name in ("start_deployment", "start_rollback", "retry_dispatch"):
        monkeypatch.setattr(github_deployments, name, github)
    monkeypatch.setattr(commands, "start_cloud_build", cloud)
    with pytest.raises(commands.HTTPException) as caught:
        if operation == "deploy":
            commands.start_deployment(
                service_name=service.service_name,
                tag_name=target.tag,
                requested_by="operator",
            )
        elif operation == "rollback":
            commands.start_rollback(
                service_name=service.service_name,
                target_deployment_id=target.id,
                requested_by="operator",
            )
        else:
            commands._retry_failed_dispatch(service, target, "key", "dispatch failed")
    assert caught.value.status_code == 409
    assert "verified health recovery" in caught.value.detail
    github.assert_not_called()
    cloud.assert_not_called()


def test_successful_probe_can_recover_legacy_circuit_without_resetting_it(monkeypatch):
    service = commands.catalog.get_service("eng-platform-api")
    circuits = orchestrator.executor_circuits
    workflow = orchestrator.config.release_orchestrator.github_health_workflow
    monkeypatch.setattr(orchestrator.config.github, "billing_owner", "owner")
    monkeypatch.setattr(
        orchestrator.github_release_control, "repository_is_private", lambda _: True
    )
    circuits.open_circuit("owner", reason="github_dispatch_without_run")
    requested = circuits.request_probe(
        "owner",
        repository=service.repository,
        workflow=workflow,
        requested_by="operator",
    )
    monkeypatch.setattr(
        orchestrator.catalog, "get_services", lambda: SimpleNamespace(services=[])
    )
    monkeypatch.setattr(
        orchestrator.github_release_control,
        "workflow_run",
        lambda *_: SimpleNamespace(
            conclusion="success", jobs=lambda: [SimpleNamespace(started_at="now")]
        ),
    )
    assert orchestrator._close_circuit_from_probe(
        service.repository,
        {
            "path": workflow,
            "event": "workflow_dispatch",
            "id": 42,
            "display_title": "eng-platform-health-" + requested["probe"]["nonce"],
        },
    )
    assert circuits.get("owner")["reason"] == "github_dispatch_without_run"
    assert circuits.get("owner")["state"] == "closed"
    assert orchestrator._provider(service) == "github_actions"


@pytest.mark.parametrize(
    "reason", ["github_dispatch_without_run", "github_actions_billing_rejection"]
)
def test_visibility_failure_cannot_bypass_open_circuit(monkeypatch, reason):
    service = commands.catalog.get_service("eng-platform-api")
    circuits = orchestrator.executor_circuits
    monkeypatch.setattr(quota.config.github, "billing_owner", "owner")
    monkeypatch.setattr(quota.config.cloud_build, "enabled", True)
    monkeypatch.setattr(quota.config.cloud_build, "mode", "auto")
    monkeypatch.setattr(
        quota.config.cloud_build, "enabled_services", (service.service_name,)
    )
    monkeypatch.setattr(quota.config.cloud_build, "cloud_build_only_services", ())
    failed = mock.Mock(side_effect=RuntimeError("metadata unavailable"))
    monkeypatch.setattr(quota, "github_client", failed)
    monkeypatch.setattr(
        orchestrator.github_release_control, "repository_is_private", failed
    )
    before, _ = circuits.open_circuit("owner", reason=reason)
    with pytest.raises(
        circuits.CircuitRecoveryRequired, match="automatic execution is paused"
    ):
        quota.should_use_cloud_build(service.service_name, service.repository)
    with pytest.raises(
        orchestrator.ReleaseOrchestratorError, match="automatic execution is paused"
    ):
        orchestrator._provider(service)
    assert circuits.get("owner") == before
