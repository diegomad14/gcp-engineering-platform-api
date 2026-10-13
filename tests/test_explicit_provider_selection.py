"""Offline provider contracts, durable intent and dispatch fences."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from eng_platform_api.config import config
from eng_platform_api.main import app
from eng_platform_api.models import (
    DeploymentCreateRequest,
    DeploymentItem,
    DeploymentRollbackRequest,
    ProviderSelection,
    ReleaseTag,
    ServiceReleaseConfig,
)
from eng_platform_api.services import (
    catalog,
    deployment_commands as commands,
    deployment_executions as deployments,
    deployment_store,
    github_deployments,
    provider_selection as selection,
    release_executions,
    release_orchestrator as releases,
)
from tests import test_release_concurrency

from tests.test_release_executions import _reserve

execution_store = test_release_concurrency.execution_store

HEAD, BASE = "a" * 40, "b" * 40
PROVIDERS = ["github_actions", "cloud_build"]


@pytest.fixture
def service(monkeypatch, tmp_path):
    value = catalog.get_service("eng-platform-api")
    assert value
    monkeypatch.setattr(catalog, "get_service", lambda _: value)
    monkeypatch.setattr(
        deployment_store, "_DEFAULT_STORE_PATH", tmp_path / "deployments.json"
    )
    monkeypatch.setattr(deployment_store, "_COLLECTION", "")
    monkeypatch.setattr(deployments, "_collection", lambda: None)
    deployments._memory.clear()
    monkeypatch.setattr(config.release_orchestrator, "enabled", True)
    monkeypatch.setattr(
        config.release_orchestrator, "enabled_services", (value.service_name,)
    )
    monkeypatch.setattr(
        config.release_orchestrator,
        "quality_python_image",
        "quality@sha256:" + "d" * 64,
    )
    monkeypatch.setattr(
        config.release_orchestrator,
        "release_planner_image",
        "planner@sha256:" + "e" * 64,
    )
    return value


@pytest.mark.parametrize("requested", PROVIDERS)
@pytest.mark.parametrize("default", PROVIDERS)
def test_request_overrides_default_separately(service, requested, default):
    service.release.executor = default
    service.deployment.executor = default
    for operation in ["release", "deploy"]:
        result = selection.select(service, requested, operation=operation)
        assert result.effective == requested
        assert result.requested == requested
        assert result.service_default == default
        assert result.source == "request" and result.locked
        assert selection.select(service, None, operation=operation).effective == default


def test_release_and_deploy_defaults_are_independent(service):
    service.release.executor = "github_actions"
    service.deployment.executor = "cloud_build"
    assert releases._provider(service) == "github_actions"
    assert (
        selection.select(service, None, operation="deploy").effective == "cloud_build"
    )
    assert ServiceReleaseConfig().executor is None
    service.release.executor = None
    assert (
        selection.select(
            service, None, operation="release", legacy_provider="cloud_build"
        ).source
        == "legacy_policy"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"tag": "v1", "provider": "auto"},
        {"tag": "v1", "provider": "other"},
        {"tag": "v1", "executor": "github_actions"},
    ],
)
def test_request_rejects_ambiguous_providers(payload):
    with pytest.raises(ValueError):
        DeploymentCreateRequest(**payload)
    with pytest.raises(ValueError):
        DeploymentRollbackRequest(provider="auto")


@pytest.mark.parametrize("provider", PROVIDERS)
def test_deployment_selection_persists_and_conflicts_before_work(
    service, monkeypatch, provider
):
    monkeypatch.setattr(catalog, "deployment_blockers", lambda _: [])
    service.deployment.executor = (
        "cloud_build" if provider == "github_actions" else "github_actions"
    )
    item = DeploymentItem(
        id="one",
        service_name=service.service_name,
        repository=service.repository,
        tag="v1",
        sha=HEAD,
    )
    monkeypatch.setattr(
        github_deployments, "get_tag", lambda *_: ReleaseTag(name="v1", sha=HEAD)
    )
    monkeypatch.setattr(commands, "_require_orchestrated_release", lambda *_: None)
    monkeypatch.setattr(github_deployments, "start_deployment", Mock(return_value=item))
    monkeypatch.setattr(
        github_deployments, "start_managed_deployment", Mock(return_value=item)
    )
    submit = Mock(side_effect=lambda _, value, **kwargs: value)
    monkeypatch.setattr(commands, "start_cloud_build", submit)
    kwargs = dict(
        service_name=service.service_name,
        tag_name="v1",
        requested_by="diegomad14",
        idempotency_key="same",
        quality_validator=lambda *_: None,
    )
    result = commands.start_deployment(**kwargs, provider=provider)
    assert result.provider_selection.effective == provider
    assert (
        deployment_store.get(result.id).provider_selection == result.provider_selection
    )
    service.deployment.executor = (
        "github_actions" if provider == "cloud_build" else "cloud_build"
    )
    assert commands.start_deployment(**kwargs).id == result.id
    with pytest.raises(HTTPException) as error:
        commands.start_deployment(
            **kwargs,
            provider="github_actions" if provider == "cloud_build" else "cloud_build",
        )
    assert error.value.status_code == 409
    assert submit.call_count == (1 if provider == "cloud_build" else 0)
    assert github_deployments.start_deployment.call_count == (
        1 if provider == "github_actions" else 0
    )


@pytest.mark.parametrize(
    "message",
    [
        "billing budget exceeded",
        "Actions minutes exhausted",
        "Resource not accessible by integration",
        "network timed out",
    ],
)
def test_github_error_stops_without_fallback_or_repo_mutation(
    service, monkeypatch, message
):
    service.deployment.executor = "cloud_build"
    item = DeploymentItem(
        id="failed",
        service_name=service.service_name,
        repository=service.repository,
        tag="v1",
        sha=HEAD,
    )
    dispatch = Mock(
        side_effect=github_deployments.GitHubDispatchError(
            item, dispatch_error=RuntimeError(message)
        )
    )
    build, legacy, modes = Mock(), Mock(), Mock()
    monkeypatch.setattr(github_deployments, "start_deployment", dispatch)
    monkeypatch.setattr(commands, "start_cloud_build", build)
    monkeypatch.setattr(commands.github_actions_quota, "should_use_cloud_build", legacy)
    monkeypatch.setattr(
        commands.github_release_control, "set_repository_execution_mode", modes
    )
    with pytest.raises(github_deployments.GitHubDispatchError):
        commands._dispatch_deploy(
            service, ReleaseTag(name="v1", sha=HEAD), "operator", "github_actions"
        )
    assert item.provider_selection.effective == "github_actions"
    build.assert_not_called()
    legacy.assert_not_called()
    modes.assert_not_called()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_manual_release_read_cancel_and_retry_never_launch_work(
    service, monkeypatch, provider
):
    submit, dispatch = Mock(), Mock()
    monkeypatch.setattr(releases.release_cloud_build, "submit", submit)
    monkeypatch.setattr(releases.github_release_control, "dispatch_release", dispatch)
    client = TestClient(app)
    path = f"/api/services/{service.service_name}/release-executions"
    response = client.post(
        path, json={"head_sha": HEAD, "base_sha": BASE, "provider": provider}
    )
    assert response.status_code == 202, response.text
    record = response.json()
    assert record["provider_selection"]["effective"] == provider
    service.release.executor = (
        "github_actions" if provider == "cloud_build" else "cloud_build"
    )
    assert (
        client.post(path, json={"head_sha": HEAD, "base_sha": BASE}).json()[
            "execution_id"
        ]
        == record["execution_id"]
    )
    assert (
        client.post(
            path,
            json={
                "head_sha": HEAD,
                "base_sha": BASE,
                "provider": service.release.executor,
            },
        ).status_code
        == 409
    )
    assert client.get(path).json()[0]["provider"] == provider
    assert (
        client.post(path + f"/{record['execution_id']}/cancel").json()["error"]
        == "Cancelled by operator"
    )
    assert (
        client.post(path, json={"head_sha": HEAD, "base_sha": BASE}).json()["status"]
        == "failed"
    )
    submit.assert_not_called()
    dispatch.assert_not_called()


def test_concurrent_conflicting_release_selection_reserves_one_provider(service):
    ready = Barrier(2)

    def start(provider):
        ready.wait(5)
        try:
            return releases.start_release(
                service_name=service.service_name,
                head_sha=HEAD,
                base_sha=BASE,
                requested_by="operator",
                provider=provider,
            )
        except HTTPException as error:
            assert error.status_code == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, PROVIDERS))
    assert len([result for result in results if result]) == 1
    assert len(release_executions._memory) == 1


def test_concurrent_deployment_requests_dispatch_once(service, monkeypatch):
    monkeypatch.setattr(
        github_deployments, "get_tag", lambda *_: ReleaseTag(name="v1", sha=HEAD)
    )
    monkeypatch.setattr(commands, "_require_orchestrated_release", lambda *_: None)
    started, finish = Event(), Event()

    def dispatch(**kwargs):
        started.set()
        assert finish.wait(5)
        return DeploymentItem(
            id="once",
            service_name=service.service_name,
            repository=service.repository,
            tag="v1",
            sha=HEAD,
        )

    mocked = Mock(side_effect=dispatch)
    monkeypatch.setattr(github_deployments, "start_deployment", mocked)
    kwargs = dict(
        service_name=service.service_name,
        tag_name="v1",
        provider="github_actions",
        requested_by="operator",
        idempotency_key="same",
        quality_validator=lambda *_: None,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(commands.start_deployment, **kwargs)
        assert started.wait(5)
        second = pool.submit(commands.start_deployment, **kwargs)
        finish.set()
        assert first.result(5).id == second.result(5).id
    mocked.assert_called_once()


def test_legacy_deployment_keeps_recorded_github_without_current_default(
    service, monkeypatch
):
    legacy = DeploymentItem(
        id="old",
        service_name=service.service_name,
        repository=service.repository,
        tag="v1",
        sha=HEAD,
        github_deployment_id=1,
        status="FAILED",
        current_stage="dispatch",
    )
    service.deployment.executor = "cloud_build"
    deployments.reserve(
        legacy.id,
        provider="github_actions",
        fingerprint="legacy-github",
        service_name=service.service_name,
        repository=service.repository,
        sha=HEAD,
        tag="v1",
        kind="deploy",
        reason="historical_record",
    )
    retried = Mock(return_value=legacy)
    monkeypatch.setattr(github_deployments, "retry_dispatch", retried)
    monkeypatch.setattr(commands, "_require_orchestrated_release", lambda *_: None)
    monkeypatch.setattr(
        commands,
        "start_cloud_build",
        Mock(side_effect=AssertionError("No alternate build")),
    )
    commands._retry_failed_dispatch(
        service, legacy, "key", "error", quality_validator=lambda *_: None
    )
    retried.assert_called_once()


def test_dispatch_admission_and_uncertain_retry_are_fenced(service):
    record = deployments.reserve(
        "one",
        provider="github_actions",
        fingerprint="fp",
        service_name=service.service_name,
        repository=service.repository,
        sha=HEAD,
        tag="v1",
        kind="deploy",
        provider_selection=ProviderSelection(
            effective="github_actions", source="request"
        ).model_dump(),
    )
    assert record["provider"] == "github_actions"
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(deployments.claim_github_dispatch, ["one"] * 4)) == 1
    deployments.save("one", status="GITHUB_UNCERTAIN")
    assert not deployments.claim_github_dispatch("one")
    with pytest.raises(ValueError, match="immutable"):
        deployments.transition_provider(
            "one",
            from_provider="github_actions",
            to_provider="cloud_build",
            reason="billing",
        )
    deployments.save("one", status="GITHUB_REJECTED")
    assert deployments.claim_github_dispatch("one")


def test_locked_release_cannot_change_provider_in_either_store(execution_store):
    record, _ = _reserve(provider="github_actions")
    release_executions.save(
        record["execution_id"],
        provider_selection=ProviderSelection(
            effective="github_actions", source="request"
        ).model_dump(),
    )
    with pytest.raises(ValueError, match="immutable"):
        release_executions.transition_to_cloud_build(
            record["execution_id"], reason="billing"
        )
    with pytest.raises(ValueError, match="immutable"):
        release_executions.save(record["execution_id"], provider="cloud_build")


def test_history_refresh_does_not_submit_on_billing(service, monkeypatch):
    from eng_platform_api.routers import deployments as routes

    item = DeploymentItem(
        id="old",
        service_name=service.service_name,
        repository=service.repository,
        tag="v1",
        status="FAILED",
        github_run_id=1,
    )
    monkeypatch.setattr(github_deployments, "refresh", lambda _: item)
    monkeypatch.setattr(
        github_deployments,
        "github_client",
        Mock(side_effect=AssertionError("No quota probe")),
    )
    build = Mock()
    monkeypatch.setattr(routes, "_start_cloud_build", build)
    assert routes._refresh(item) == item
    build.assert_not_called()


def test_github_choice_validates_its_readiness_instead_of_cloud_default(service):
    service.deployment.executor = "cloud_build"
    service.deployment_ready = False
    service.deployment_blockers = ["Cloud Build executor is disabled"]
    assert commands._service_or_404(
        service.service_name, "github_actions"
    ).deployment_ready
    service.deployment.health_path = ""
    with pytest.raises(HTTPException, match="health_path"):
        commands._service_or_404(service.service_name, "github_actions")


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("reject", [False, True])
def test_manual_release_dispatch_is_once_and_never_falls_back(
    service, monkeypatch, provider, reject
):
    from eng_platform_api.services import log_catalog

    monkeypatch.setattr(log_catalog, "load_catalog", lambda: [service.model_dump()])
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(release_executions, "_collection", lambda: None)
    monkeypatch.setattr(
        releases.github_release_control, "current_default_sha", lambda _: HEAD
    )
    monkeypatch.setattr(
        releases.github_release_control, "is_fast_forward", lambda *_: True
    )
    monkeypatch.setattr(releases, "_start_checks", Mock())
    github = Mock(side_effect=RuntimeError("Payment required") if reject else None)
    cloud = Mock(
        side_effect=RuntimeError("rejected")
        if reject
        else lambda execution, _: execution
    )
    monkeypatch.setattr(releases.github_release_control, "dispatch_release", github)
    monkeypatch.setattr(releases, "_submit_if_managed", cloud)
    kwargs = dict(
        service_name=service.service_name,
        head_sha=HEAD,
        base_sha=BASE,
        requested_by="operator",
        provider=provider,
    )
    if reject:
        with pytest.raises(HTTPException) as error:
            releases.start_release(**kwargs)
        assert error.value.status_code == 502
    else:
        releases.start_release(**kwargs)
    again = releases.start_release(**kwargs)
    assert again["provider"] == provider
    assert again["status"] == (
        "failed"
        if reject
        else "waiting_github"
        if provider == "github_actions"
        else "submission_pending"
    )
    assert github.call_count == (1 if provider == "github_actions" else 0)
    assert cloud.call_count == (1 if provider == "cloud_build" else 0)


def test_github_only_startup_has_no_cloud_build_dependency():
    import os
    from unittest.mock import patch
    from eng_platform_api.config import load_config

    image = "local/test@sha256:" + "d" * 64
    with patch.dict(
        os.environ,
        {
            "ENG_PLATFORM_MOCK_MODE": "true",
            "ENG_PLATFORM_PRIVATE_EXECUTOR_MODE": "github_actions",
            "ENG_PLATFORM_RELEASE_ORCHESTRATOR_ENABLED": "true",
            "ENG_PLATFORM_GITHUB_WEBHOOK_SECRET": "offline-fixture",
            "ENG_PLATFORM_GITHUB_APP_ID": "1",
            "ENG_PLATFORM_GITHUB_INSTALLATION_ID": "1",
            "ENG_PLATFORM_GITHUB_PRIVATE_KEY": "offline-fixture",
            "ENG_PLATFORM_API_URL": "https://offline.invalid",
            "ENG_PLATFORM_RELEASE_RECONCILER_SERVICE_ACCOUNT": "reconciler@offline.iam.gserviceaccount.com",
            "ENG_PLATFORM_QUALITY_BUCKET": "offline-fixture",
            "ENG_PLATFORM_QUALITY_NODE_IMAGE": image,
            "ENG_PLATFORM_QUALITY_PYTHON_IMAGE": image,
            "ENG_PLATFORM_RELEASE_PLANNER_IMAGE": image,
        },
        clear=True,
    ):
        loaded = load_config()
    assert loaded.release_orchestrator.enabled
    assert not loaded.cloud_build.enabled
    assert loaded.release_orchestrator.private_executor_mode == "github_actions"


def test_transactional_deployment_command_fence_rejects_parallel_owner(monkeypatch):
    from types import SimpleNamespace
    from google.cloud import firestore
    from tests.test_release_executions import _Collection, _Transaction

    locks = _Collection()

    class Transaction(_Transaction):
        def delete(self, document):
            document.value = None

    collection = _Collection()
    collection._client = SimpleNamespace(
        transaction=Transaction, collection=lambda _: locks
    )
    monkeypatch.setattr(deployment_store, "_firestore_collection", lambda: collection)
    monkeypatch.setattr(firestore, "transactional", lambda action: action)
    with deployment_store.command_guard("service"):
        with pytest.raises(HTTPException) as conflict:
            with deployment_store.command_guard("service"):
                pytest.fail("A second owner entered the command boundary")
        assert conflict.value.status_code == 409
    with deployment_store.command_guard("service"):
        pass


@pytest.mark.parametrize(
    "state", ["GITHUB_UNCERTAIN", "GITHUB_DISPATCHING", "GITHUB_ACCEPTED"]
)
def test_pending_dispatch_cannot_retry_even_after_default_changes(
    service, monkeypatch, state
):
    item = DeploymentItem(
        id="uncertain",
        service_name=service.service_name,
        repository=service.repository,
        tag="v1",
        sha=HEAD,
        status="FAILED",
        current_stage="dispatch",
        provider_selection=ProviderSelection(
            effective="github_actions", source="request"
        ),
    )
    monkeypatch.setattr(
        deployments, "get", lambda _: {"provider": "github_actions", "status": state}
    )
    monkeypatch.setattr(commands, "_require_orchestrated_release", lambda *_: None)
    retry, build = Mock(), Mock()
    monkeypatch.setattr(github_deployments, "retry_dispatch", retry)
    monkeypatch.setattr(commands, "start_cloud_build", build)
    with pytest.raises(HTTPException) as conflict:
        commands._retry_failed_dispatch(
            service, item, "same", "error", quality_validator=lambda *_: None
        )
    assert conflict.value.status_code == 409
    retry.assert_not_called()
    build.assert_not_called()
