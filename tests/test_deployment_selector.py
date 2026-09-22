"""Private provider selection remains invisible and strictly classified."""

from unittest import mock

import pytest

from eng_platform_api.models import DeploymentItem, ReleaseTag
from eng_platform_api.routers import deployments
from eng_platform_api.services import catalog, cloud_build, github_deployments


@pytest.fixture
def service():
    value = catalog.get_service("eng-platform-api")
    assert value is not None
    return value


def _item(**changes):
    values = {
        "id": "deployment-1",
        "service_name": "eng-platform-api",
        "repository": "diegomad14/gcp-engineering-platform-api",
        "tag": "v1.2.3",
        "sha": "a" * 40,
        "github_deployment_id": 123,
    }
    values.update(changes)
    return DeploymentItem(**values)


def test_start_cloud_build_revokes_github_and_projects_status(monkeypatch, service):
    item = _item()
    revoke = mock.Mock()
    transition = mock.Mock()
    status = mock.Mock()
    monkeypatch.setattr(
        deployments.deployment_executions,
        "get",
        lambda _: {"provider": "github_actions", "authorization_jti": "ticket"},
    )
    monkeypatch.setattr(deployments.release_authorization_store, "revoke", revoke)
    monkeypatch.setattr(
        deployments.deployment_executions, "transition_provider", transition
    )
    monkeypatch.setattr(
        deployments.cloud_build,
        "submit",
        lambda value, _service, reason: value.model_copy(
            update={"logs_url": "https://logs.invalid/build"}
        ),
    )
    monkeypatch.setattr(deployments.github_deployments, "set_managed_status", status)

    result = deployments._start_cloud_build(service, item, reason="quota")

    assert result.logs_url == "https://logs.invalid/build"
    revoke.assert_called_once_with("ticket")
    transition.assert_called_once_with(
        item.id,
        from_provider="github_actions",
        to_provider="cloud_build",
        reason="quota",
    )
    status.assert_called_once()


def test_start_cloud_build_converts_provider_error(monkeypatch, service):
    item = _item()
    monkeypatch.setattr(deployments.deployment_executions, "get", lambda _: None)
    monkeypatch.setattr(
        deployments.cloud_build,
        "submit",
        mock.Mock(side_effect=cloud_build.CloudBuildError("disabled")),
    )

    with pytest.raises(github_deployments.GitHubDispatchError) as raised:
        deployments._start_cloud_build(service, item, reason="quota")

    assert raised.value.item.status == "FAILED"
    assert raised.value.item.current_stage == "dispatch"
    assert raised.value.item.error == "disabled"


def test_dispatch_deploy_uses_preflight_cloud_build(monkeypatch, service):
    managed = _item()
    tag = ReleaseTag(name=managed.tag, sha=managed.sha)
    monkeypatch.setattr(
        deployments.github_actions_quota, "should_use_cloud_build", lambda *_: True
    )
    monkeypatch.setattr(
        deployments.github_deployments,
        "start_managed_deployment",
        lambda **_kwargs: managed,
    )
    start_cloud = mock.Mock(return_value=managed)
    monkeypatch.setattr(deployments, "_start_cloud_build", start_cloud)

    assert deployments._dispatch_deploy(service, tag, "operator") is managed
    start_cloud.assert_called_once_with(
        service, managed, reason="github_quota_preflight"
    )


@pytest.mark.parametrize("quota_error", [True, False])
def test_dispatch_deploy_only_falls_back_for_explicit_quota(
    monkeypatch, service, quota_error
):
    failed = _item(status="FAILED", current_stage="dispatch")
    error = github_deployments.GitHubDispatchError(failed)
    tag = ReleaseTag(name=failed.tag, sha=failed.sha)
    monkeypatch.setattr(
        deployments.github_actions_quota, "should_use_cloud_build", lambda *_: False
    )
    monkeypatch.setattr(
        deployments.github_deployments,
        "start_deployment",
        mock.Mock(side_effect=error),
    )
    monkeypatch.setattr(
        deployments.github_actions_quota,
        "is_quota_error",
        lambda _: quota_error,
    )
    start_cloud = mock.Mock(return_value=failed)
    monkeypatch.setattr(deployments, "_start_cloud_build", start_cloud)

    if quota_error:
        assert deployments._dispatch_deploy(service, tag, "operator") is failed
        start_cloud.assert_called_once_with(
            service, failed, reason="github_quota_dispatch"
        )
    else:
        with pytest.raises(github_deployments.GitHubDispatchError):
            deployments._dispatch_deploy(service, tag, "operator")
        start_cloud.assert_not_called()


def test_dispatch_rollback_preflight_preserves_target(monkeypatch, service):
    target = _item(production_revision="revision-7")
    managed = _item(kind="rollback", production_revision="revision-7")
    monkeypatch.setattr(
        deployments.github_actions_quota, "should_use_cloud_build", lambda *_: True
    )
    start_managed = mock.Mock(return_value=managed)
    monkeypatch.setattr(
        deployments.github_deployments,
        "start_managed_deployment",
        start_managed,
    )
    monkeypatch.setattr(
        deployments, "_start_cloud_build", lambda *_args, **_kwargs: managed
    )

    assert deployments._dispatch_rollback(service, target, "operator") is managed
    assert start_managed.call_args.kwargs["target_revision"] == "revision-7"
    assert start_managed.call_args.kwargs["kind"] == "rollback"


def test_refresh_projects_cloud_terminal_state(monkeypatch):
    item = _item()
    terminal = item.model_copy(
        update={
            "status": "SUCCEEDED",
            "production_url": "https://service.invalid",
            "logs_url": "https://logs.invalid/build",
        }
    )
    monkeypatch.setattr(
        deployments.deployment_executions,
        "get",
        lambda _: {"provider": "cloud_build"},
    )
    monkeypatch.setattr(deployments.cloud_build, "refresh", lambda _: terminal)
    status = mock.Mock()
    monkeypatch.setattr(deployments.github_deployments, "set_managed_status", status)

    assert deployments._refresh(item) is terminal
    assert status.call_args.kwargs["state"] == "success"


def test_refresh_reacts_only_to_verified_startup_quota(monkeypatch, service):
    item = _item(status="FAILED", github_run_id=99)
    run = object()
    repo = mock.Mock()
    repo.get_workflow_run.return_value = run
    client = mock.Mock()
    client.get_repo.return_value = repo
    monkeypatch.setattr(deployments.deployment_executions, "get", lambda _: None)
    monkeypatch.setattr(deployments.github_deployments, "refresh", lambda _: item)
    monkeypatch.setattr(deployments.github_deployments, "github_client", lambda: client)
    monkeypatch.setattr(
        deployments.github_actions_quota,
        "is_reactive_quota_failure",
        lambda *_: True,
    )
    monkeypatch.setattr(deployments, "_service_or_404", lambda _: service)
    start_cloud = mock.Mock(return_value=item)
    monkeypatch.setattr(deployments, "_start_cloud_build", start_cloud)

    assert deployments._refresh(item) is item
    start_cloud.assert_called_once_with(service, item, reason="github_quota_startup")
