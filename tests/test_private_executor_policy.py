"""Explicit private execution policy is not a fabricated billing fallback."""

import os
from types import SimpleNamespace
from unittest import mock

import pytest

from eng_platform_api.config import ReleaseOrchestratorConfig, config, load_config
from eng_platform_api.models import CatalogService, DeploymentItem, ReleaseTag
from eng_platform_api.services import deployment_commands as commands
from eng_platform_api.services import github_actions_quota as quota
from eng_platform_api.services import release_orchestrator as orchestrator


@pytest.fixture(autouse=True)
def explicit_policy(monkeypatch):
    monkeypatch.setattr(
        config.release_orchestrator, "private_executor_mode", "cloud_build"
    )


def _service():
    return CatalogService(
        service_name="example-service",
        management_mode="managed",
        repository="owner/private",
        owner="platform",
        project_id="test-project",
        region="us-central1",
    )


@pytest.mark.parametrize("mode", [None, "auto", "cloud_build"])
def test_config_loads_explicit_private_policy(mode):
    environment = {"ENG_PLATFORM_MOCK_MODE": "true"}
    if mode is not None:
        environment["ENG_PLATFORM_PRIVATE_EXECUTOR_MODE"] = mode
    with mock.patch.dict(os.environ, environment, clear=True):
        assert load_config().release_orchestrator.private_executor_mode == (
            mode or "cloud_build"
        )
    assert ReleaseOrchestratorConfig().private_executor_mode == "cloud_build"


@pytest.mark.parametrize("mode", ["", "github_actions", "unknown"])
def test_config_rejects_invalid_private_policy(mode):
    with mock.patch.dict(
        os.environ,
        {
            "ENG_PLATFORM_MOCK_MODE": "true",
            "ENG_PLATFORM_PRIVATE_EXECUTOR_MODE": mode,
        },
        clear=True,
    ):
        with pytest.raises(ValueError, match="ENG_PLATFORM_PRIVATE_EXECUTOR_MODE"):
            load_config()


@pytest.mark.parametrize(
    "private,expected", [(True, "cloud_build"), (False, "github_actions")]
)
def test_release_selection_never_reads_or_changes_billing(
    monkeypatch, private, expected
):
    monkeypatch.setattr(
        orchestrator.github_release_control, "repository_is_private", lambda _: private
    )
    with (
        mock.patch.object(orchestrator.executor_circuits, "get") as circuit,
        mock.patch.object(orchestrator.executor_circuits, "open_circuit") as opened,
        mock.patch.object(orchestrator.github_actions_quota, "current_usage") as usage,
        mock.patch.object(
            orchestrator.github_release_control, "set_repository_execution_mode"
        ) as hint,
    ):
        assert orchestrator._provider(_service()) == expected
    circuit.assert_not_called()
    opened.assert_not_called()
    usage.assert_not_called()
    hint.assert_not_called()


def test_unknown_release_visibility_is_fail_closed(monkeypatch):
    monkeypatch.setattr(
        orchestrator.github_release_control,
        "repository_is_private",
        mock.Mock(side_effect=RuntimeError("visibility unavailable")),
    )
    with mock.patch.object(orchestrator.executor_circuits, "get") as circuit:
        with pytest.raises(orchestrator.ReleaseOrchestratorError, match="visibility"):
            orchestrator._provider(_service())
    circuit.assert_not_called()


@pytest.mark.parametrize(
    "private,enabled,enrolled,mode,expected",
    [
        (True, True, True, "auto", True),
        (False, True, True, "auto", False),
        (True, False, True, "auto", False),
        (True, True, False, "auto", False),
        (True, True, True, "github_actions", False),
        (True, True, True, "cloud_build", True),
    ],
)
def test_deploy_policy_preserves_enrollment_and_explicit_overrides(
    monkeypatch, private, enabled, enrolled, mode, expected
):
    monkeypatch.setattr(quota.catalog, "get_service", lambda _: None)
    monkeypatch.setattr(config.cloud_build, "enabled", enabled)
    monkeypatch.setattr(
        config.cloud_build, "enabled_services", ("example-service",) if enrolled else ()
    )
    monkeypatch.setattr(config.cloud_build, "cloud_build_only_services", ())
    monkeypatch.setattr(config.cloud_build, "mode", mode)
    monkeypatch.setattr(
        quota,
        "github_client",
        lambda: SimpleNamespace(get_repo=lambda _: SimpleNamespace(private=private)),
    )
    with mock.patch.object(quota.executor_circuits, "get") as circuit:
        assert quota.should_use_cloud_build("example-service", "owner/repo") is expected
    circuit.assert_not_called()


def test_unknown_deploy_visibility_is_fail_closed(monkeypatch):
    monkeypatch.setattr(quota.catalog, "get_service", lambda _: None)
    monkeypatch.setattr(config.cloud_build, "enabled", True)
    monkeypatch.setattr(config.cloud_build, "enabled_services", ("example-service",))
    monkeypatch.setattr(config.cloud_build, "cloud_build_only_services", ())
    monkeypatch.setattr(config.cloud_build, "mode", "auto")
    monkeypatch.setattr(
        quota, "github_client", mock.Mock(side_effect=RuntimeError("unavailable"))
    )
    with mock.patch.object(quota.executor_circuits, "get") as circuit:
        with pytest.raises(
            quota.executor_circuits.CircuitRecoveryRequired, match="visibility"
        ):
            quota.should_use_cloud_build("example-service", "owner/repo")
    circuit.assert_not_called()


def test_probe_closes_billing_without_rewriting_executor_hints(monkeypatch):
    workflow = config.release_orchestrator.github_health_workflow
    circuit = {
        "state": "open",
        "probe": {
            "status": "requested",
            "repository": "owner/private",
            "workflow": workflow,
            "nonce": "bound",
        },
    }
    monkeypatch.setattr(orchestrator.executor_circuits, "get", lambda _: circuit)
    monkeypatch.setattr(
        orchestrator.github_release_control,
        "workflow_run",
        lambda *_: SimpleNamespace(
            conclusion="success", jobs=lambda: [SimpleNamespace(started_at="now")]
        ),
    )
    with (
        mock.patch.object(orchestrator.executor_circuits, "record_probe"),
        mock.patch.object(
            orchestrator.executor_circuits, "close_after_successful_probe"
        ) as close,
        mock.patch.object(
            orchestrator.github_release_control, "set_repository_execution_mode"
        ) as hint,
        mock.patch.object(orchestrator.catalog, "get_services") as services,
    ):
        assert orchestrator._close_circuit_from_probe(
            "owner/private",
            {
                "path": workflow,
                "event": "workflow_dispatch",
                "display_title": "eng-platform-health-bound",
                "id": 42,
            },
        )
    close.assert_called_once_with("owner", run_id="42")
    hint.assert_not_called()
    services.assert_not_called()


def test_policy_change_reuses_github_reservation_without_cloud_build(monkeypatch):
    service = _service()
    profile = SimpleNamespace(fingerprint=lambda: "profile")
    previous = {
        "execution_id": "existing",
        "service_name": service.service_name,
        "operation": "pr_quality",
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "profile_hash": "profile",
        "executor_digest": "image",
        "policy_hash": "policy",
        "planner_hash": "",
        "provider": "github_actions",
    }
    monkeypatch.setattr(orchestrator, "profile_for", lambda _: profile)
    monkeypatch.setattr(orchestrator, "executor_image", lambda _: "image")
    monkeypatch.setattr(orchestrator, "_policy_hash", lambda _: "policy")
    monkeypatch.setattr(
        orchestrator.release_executions,
        "list_for_repository",
        lambda *_, **__: [previous],
    )
    with (
        mock.patch.object(orchestrator.release_executions, "reserve") as reserve,
        mock.patch.object(orchestrator.release_cloud_build, "submit") as submit,
    ):
        execution, created = orchestrator._reserve(
            service=service,
            operation="pr_quality",
            head_sha="a" * 40,
            base_sha="b" * 40,
            branch="feature",
            delivery_id="replay",
            provider="cloud_build",
        )
        assert execution is previous
        assert created is False
        assert orchestrator._submit_if_managed(execution, service) is previous
    reserve.assert_not_called()
    submit.assert_not_called()


def test_explicit_deployment_reason_does_not_touch_billing_or_hints():
    with (
        mock.patch.object(commands.executor_circuits, "is_open") as circuit,
        mock.patch.object(commands, "_open_billing_circuit") as opened,
    ):
        assert commands._cloud_build_preflight_reason(_service()) == (
            "explicit_executor_policy"
        )
    circuit.assert_not_called()
    opened.assert_not_called()


@pytest.mark.parametrize("kind", ["deploy", "rollback"])
def test_explicit_dispatch_uses_policy_reason(monkeypatch, kind):
    service = _service()
    item = DeploymentItem(
        id="deployment-1",
        service_name=service.service_name,
        repository=service.repository,
        tag="v1.0.0",
        sha="a" * 40,
        production_revision="previous",
    )
    monkeypatch.setattr(
        commands.github_actions_quota, "should_use_cloud_build", lambda *_: True
    )
    with (
        mock.patch.object(
            commands.github_deployments,
            "start_managed_deployment",
            return_value=item,
        ),
        mock.patch.object(commands, "start_cloud_build", return_value=item) as submit,
        mock.patch.object(commands, "_open_billing_circuit") as opened,
    ):
        if kind == "deploy":
            result = commands._dispatch_deploy(
                service, ReleaseTag(name=item.tag, sha=item.sha), "operator"
            )
        else:
            result = commands._dispatch_rollback(service, item, "operator")
    assert result is item
    submit.assert_called_once_with(service, item, reason="explicit_executor_policy")
    opened.assert_not_called()


@pytest.mark.parametrize("provider", ["github_actions", "cloud_build", None])
def test_failed_dispatch_keeps_reserved_provider(monkeypatch, provider):
    service = _service()
    item = DeploymentItem(
        id="deployment-1",
        service_name=service.service_name,
        repository=service.repository,
        tag="v1.0.0",
        sha="a" * 40,
        kind="rollback",
        status="FAILED",
        current_stage="dispatch",
    )
    monkeypatch.setattr(
        commands.deployment_executions,
        "get",
        lambda _: {"provider": provider} if provider else None,
    )
    monkeypatch.setattr(commands.deployment_store, "save", lambda value, _: value)
    with (
        mock.patch.object(
            commands.github_actions_quota, "should_use_cloud_build"
        ) as select,
        mock.patch.object(commands, "start_cloud_build", return_value=item) as cb,
        mock.patch.object(
            commands.github_deployments, "retry_dispatch", return_value=item
        ) as gh,
    ):
        if provider is None:
            with pytest.raises(commands.HTTPException) as error:
                commands._retry_failed_dispatch(service, item, "key", "failed")
            assert error.value.status_code == 409
        else:
            assert (
                commands._retry_failed_dispatch(service, item, "key", "failed") is item
            )
    select.assert_not_called()
    assert cb.call_count == int(provider == "cloud_build")
    assert gh.call_count == int(provider == "github_actions")


@pytest.mark.parametrize("plane", ["release", "deployment"])
def test_real_billing_rejection_does_not_rewrite_hints_during_drain(monkeypatch, plane):
    monkeypatch.setattr(
        orchestrator.github_release_control, "repository_is_private", lambda _: True
    )
    with (
        mock.patch.object(
            orchestrator.executor_circuits,
            "open_circuit",
            return_value=({"state": "open"}, True),
        ) as opened,
        mock.patch.object(
            orchestrator.github_release_control, "set_repository_execution_mode"
        ) as hint,
        mock.patch.object(orchestrator.catalog, "get_services") as services,
    ):
        if plane == "release":
            orchestrator._open_circuit(
                repository="owner/private",
                run_id="42",
                reason="github_actions_billing_rejection",
                evidence="real pre-start rejection",
            )
        else:
            commands._open_billing_circuit(
                _service(),
                reason="github_actions_billing_rejection",
                evidence="real pre-start rejection",
            )
    opened.assert_called_once()
    assert opened.call_args.kwargs["reason"] == "github_actions_billing_rejection"
    hint.assert_not_called()
    services.assert_not_called()
