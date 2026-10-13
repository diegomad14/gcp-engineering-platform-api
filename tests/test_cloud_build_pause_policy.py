"""Final creation admission, using real commands and transports with no network."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from eng_platform_api.config import config, load_config
from eng_platform_api.models import DeploymentItem, ProviderSelection
from eng_platform_api.services import (
    catalog,
    cloud_build,
    cloud_build_policy,
    deployment_commands,
    deployment_executions,
    github_deployments,
    release_cloud_build,
    release_executions,
    release_quality_bootstrap,
)
from tests.test_release_executions import _reserve


@pytest.fixture
def paused(monkeypatch):
    monkeypatch.setattr(config.cloud_build, "enabled", True)
    monkeypatch.setattr(config.cloud_build, "new_builds_enabled", False)
    monkeypatch.setattr(deployment_executions, "_collection", lambda: None)
    deployment_executions._memory.clear()
    return catalog.get_service("eng-platform-api")


def item(service, **values):
    return DeploymentItem(
        id="existing",
        service_name=service.service_name,
        repository=service.repository,
        tag="v1",
        sha="a" * 40,
        **values,
    )


def test_new_admission_is_closed_by_default(monkeypatch):
    monkeypatch.delenv("ENG_PLATFORM_CLOUD_BUILD_NEW_BUILDS_ENABLED", raising=False)
    assert not load_config().cloud_build.new_builds_enabled


@pytest.mark.parametrize("enabled,approved", [(False, True), (True, False)])
def test_both_feature_and_creation_approval_are_required(
    monkeypatch, enabled, approved
):
    monkeypatch.setattr(config.cloud_build, "enabled", enabled)
    monkeypatch.setattr(config.cloud_build, "new_builds_enabled", approved)
    with pytest.raises(HTTPException) as error:
        cloud_build_policy.require_new_builds()
    assert error.value.status_code == 409


def test_deployment_pause_precedes_claims_revocation_transition_and_post(
    paused, monkeypatch
):
    from eng_platform_api.services import mcp_grants, release_authorization_store

    value = item(paused)
    deployment_executions.reserve(
        value.id,
        provider="github_actions",
        fingerprint="f",
        service_name=value.service_name,
        repository=value.repository,
        sha=value.sha,
        tag=value.tag,
        kind="deploy",
        reason="historical",
        authorization_jti="old-jti",
    )
    revoked, claims, session = Mock(), Mock(), Mock()
    monkeypatch.setattr(release_authorization_store, "revoke", revoked)
    monkeypatch.setattr(mcp_grants, "release_claims", claims)
    monkeypatch.setattr(cloud_build, "_session", session)
    for call in [
        lambda: deployment_commands.start_cloud_build(paused, value, reason="legacy"),
        lambda: cloud_build.submit(value, paused, reason="legacy"),
    ]:
        with pytest.raises(HTTPException):
            call()
    assert deployment_executions.get(value.id)["provider"] == "github_actions"
    revoked.assert_not_called()
    claims.assert_not_called()
    session.assert_not_called()


def test_release_submission_pause_precedes_request_claim_and_post(paused, monkeypatch):
    execution, _ = _reserve(provider="cloud_build")
    monkeypatch.setattr(release_cloud_build, "_matching_build", lambda _: None)
    request, claim, session = Mock(), Mock(), Mock()
    monkeypatch.setattr(release_cloud_build, "build_request", request)
    monkeypatch.setattr(release_executions, "claim_submission", claim)
    monkeypatch.setattr(release_cloud_build, "_session", session)
    with pytest.raises(HTTPException):
        release_cloud_build.submit(execution["execution_id"], paused)
    request.assert_not_called()
    claim.assert_not_called()
    session.assert_not_called()
    assert (
        release_executions.get(execution["execution_id"])["status"]
        == "submission_pending"
    )


def test_bootstrap_pause_precedes_authorization_claim_and_post(paused, monkeypatch):
    session, claim, authorize = Mock(), Mock(), Mock()
    monkeypatch.setattr(release_cloud_build, "_bootstrap_session", session)
    monkeypatch.setattr(release_executions, "claim_quality_bootstrap_dispatch", claim)
    with pytest.raises(HTTPException):
        release_cloud_build.submit_quality_bootstrap(
            {}, request={}, dispatch_token="x", reauthorize=authorize
        )
    session.assert_not_called()
    claim.assert_not_called()
    authorize.assert_not_called()
    monkeypatch.setattr(
        release_executions, "get_quality_bootstrap_ticket", lambda: None
    )
    monkeypatch.setattr(release_executions, "get", lambda _: {})
    reserve = Mock()
    monkeypatch.setattr(release_executions, "reserve_quality_bootstrap", reserve)
    with pytest.raises(HTTPException):
        release_quality_bootstrap.request_quality_bootstrap(
            "old", idempotency_key="valid", actor="diegomad14", reauthorize=lambda: True
        )
    reserve.assert_not_called()


def test_pause_preserves_legacy_provider_and_planner_state(paused):
    execution, _ = _reserve(provider="github_actions")
    before = release_executions.get(execution["execution_id"])
    with pytest.raises(HTTPException):
        release_executions.transition_to_cloud_build(
            execution["execution_id"], reason="legacy"
        )
    with pytest.raises(HTTPException):
        release_executions.stage_planner_retry(
            execution["execution_id"], failed_build_id="old"
        )
    assert release_executions.get(execution["execution_id"]) == before


def test_existing_cloud_builds_remain_readable_without_new_submission(
    paused, monkeypatch
):
    execution, _ = _reserve(provider="cloud_build")
    release_executions.bind_build(execution["execution_id"], build_id="already-created")
    session = Mock()
    monkeypatch.setattr(release_cloud_build, "_session", session)
    assert (
        release_cloud_build.submit(execution["execution_id"], paused)["build_id"]
        == "already-created"
    )
    value = item(paused)
    deployment_executions.reserve(
        value.id,
        provider="cloud_build",
        fingerprint="f",
        service_name=value.service_name,
        repository=value.repository,
        sha=value.sha,
        tag=value.tag,
        kind="deploy",
        reason="historical",
    )
    deployment_executions.save(
        value.id, build_id="already-created", log_url="existing-log"
    )
    assert (
        cloud_build.submit(value, paused, reason="read-existing").logs_url
        == "existing-log"
    )
    session.assert_not_called()


@pytest.mark.parametrize("github_deployment_id,github_run_id", [(1, None), (1, 3)])
def test_unknown_legacy_provider_is_not_inferred_from_github_objects(
    paused, monkeypatch, github_deployment_id, github_run_id
):
    value = item(
        paused,
        github_deployment_id=github_deployment_id,
        github_run_id=github_run_id,
        status="FAILED",
        current_stage="dispatch",
    )
    dispatch, submit = Mock(), Mock()
    monkeypatch.setattr(github_deployments, "retry_dispatch", dispatch)
    monkeypatch.setattr(deployment_commands, "start_cloud_build", submit)
    monkeypatch.setattr(
        deployment_commands, "_require_orchestrated_release", lambda *_: None
    )
    with pytest.raises(HTTPException) as error:
        deployment_commands._retry_failed_dispatch(
            paused, value, "old-key", "error", quality_validator=lambda *_: None
        )
    assert error.value.status_code == 409
    dispatch.assert_not_called()
    submit.assert_not_called()


@pytest.mark.parametrize("kind", ["deploy", "rollback"])
def test_real_github_retry_claim_and_authorization_are_composed_once(
    paused, monkeypatch, kind
):
    value = item(
        paused,
        kind=kind,
        github_deployment_id=1,
        status="FAILED",
        current_stage="dispatch",
        production_revision="old-revision",
        provider_selection=ProviderSelection(
            requested="github_actions", effective="github_actions", source="request"
        ),
    )
    deployment_executions.reserve(
        value.id,
        provider="github_actions",
        fingerprint="f",
        service_name=value.service_name,
        repository=value.repository,
        sha=value.sha,
        tag=value.tag,
        kind=kind,
        reason="explicit",
        provider_selection=value.provider_selection.model_dump(),
    )
    deployment_executions.save(value.id, status="GITHUB_REJECTED")
    client, workflow, authorize = (
        Mock(),
        Mock(),
        Mock(return_value=("token", {"jti": "new"})),
    )
    client.get_repo.return_value.get_workflow.return_value = workflow
    monkeypatch.setattr(github_deployments, "github_client", lambda: client)
    monkeypatch.setattr(github_deployments.release_authorization, "issue", authorize)
    started, finish = Event(), Event()

    def remote(**_):
        started.set()
        assert finish.wait(5)
        return True

    workflow.create_dispatch.side_effect = remote
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            github_deployments.retry_dispatch,
            service=paused,
            item=value.model_copy(deep=True),
        )
        assert started.wait(5)
        second = pool.submit(
            github_deployments.retry_dispatch,
            service=paused,
            item=value.model_copy(deep=True),
        )
        with pytest.raises(ValueError):
            second.result(5)
        finish.set()
        assert first.result(5).status == "QUEUED"
    workflow.create_dispatch.assert_called_once()
    authorize.assert_called_once()
    record = deployment_executions.get(value.id)
    assert (
        record["status"] == "GITHUB_ACCEPTED" and record["authorization_jti"] == "new"
    )
    assert record["provider"] == "github_actions"


@pytest.mark.parametrize("failure_point", ["bind", "after_dispatch"])
def test_real_command_store_failure_keeps_admission_and_cannot_dispatch_again(
    paused, monkeypatch, tmp_path, failure_point
):
    from eng_platform_api.models import ReleaseTag
    from eng_platform_api.services import deployment_store, log_catalog

    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(log_catalog, "load_catalog", lambda: [paused.model_dump()])
    monkeypatch.setattr(deployment_store, "_COLLECTION", "")
    monkeypatch.setattr(
        deployment_store, "_DEFAULT_STORE_PATH", tmp_path / "requests.json"
    )
    monkeypatch.setattr(catalog, "get_service", lambda _: paused)
    monkeypatch.setattr(
        github_deployments, "get_tag", lambda *_: ReleaseTag(name="v1", sha="a" * 40)
    )
    monkeypatch.setattr(
        deployment_commands, "_require_orchestrated_release", lambda *_: None
    )
    client, workflow = Mock(), Mock()
    client.get_repo.return_value.create_deployment.return_value.id = 101
    client.get_repo.return_value.get_workflow.return_value = workflow
    workflow.create_dispatch.return_value = True
    monkeypatch.setattr(github_deployments, "github_client", lambda: client)
    monkeypatch.setattr(
        github_deployments.release_authorization,
        "issue",
        lambda **_: ("token", {"jti": "jti"}),
    )
    original = deployment_store.save

    def save(value, key):
        accepted = (deployment_executions.get(value.id) or {}).get(
            "status"
        ) == "GITHUB_ACCEPTED"
        if (failure_point == "bind" and value.id == "101") or (
            failure_point == "after_dispatch" and accepted
        ):
            raise OSError("simulated persistent store failure")
        return original(value, key)

    monkeypatch.setattr(deployment_store, "save", save)
    kwargs = dict(
        service_name=paused.service_name,
        tag_name="v1",
        requested_by="diegomad14",
        idempotency_key="original",
        provider="github_actions",
        quality_validator=lambda *_: None,
    )
    with pytest.raises(HTTPException):
        deployment_commands.start_deployment(**kwargs)
    existing = deployment_commands.start_deployment(**kwargs)
    assert existing.provider_selection.effective == "github_actions"
    assert existing.status == "QUEUED"
    assert workflow.create_dispatch.call_count == (
        1 if failure_point == "after_dispatch" else 0
    )
    client.get_repo.return_value.create_deployment.assert_called_once()
    with pytest.raises(HTTPException) as error:
        deployment_commands.start_deployment(
            **{**kwargs, "idempotency_key": "different"}
        )
    assert error.value.status_code == 409


def test_rollback_key_binds_target_id_revision_and_original_provider(
    paused, monkeypatch, tmp_path
):
    from eng_platform_api.services import deployment_store

    monkeypatch.setattr(deployment_store, "_COLLECTION", "")
    monkeypatch.setattr(
        deployment_store, "_DEFAULT_STORE_PATH", tmp_path / "rollbacks.json"
    )
    target = item(paused, status="SUCCEEDED", production_revision="revision-a")
    second = target.model_copy(
        update={"id": "second", "production_revision": "revision-b"}
    )
    deployment_store.save(target, "target-a")
    deployment_store.save(second, "target-b")
    retry = item(
        paused,
        kind="rollback",
        status="FAILED",
        current_stage="dispatch",
        production_revision="revision-a",
        rollback_target_deployment_id=target.id,
        provider_selection=ProviderSelection(
            effective="github_actions", source="legacy_execution"
        ),
    )
    retry.id = "retry"
    deployment_store.save(retry, "stable")
    paused.deployment.executor = "cloud_build"
    monkeypatch.setattr(catalog, "get_service", lambda _: paused)
    dispatch = Mock(return_value=retry)
    monkeypatch.setattr(github_deployments, "retry_dispatch", dispatch)
    kwargs = dict(
        service_name=paused.service_name,
        requested_by="diegomad14",
        idempotency_key="stable",
    )
    with pytest.raises(HTTPException) as error:
        deployment_commands.start_rollback(target_deployment_id=second.id, **kwargs)
    assert error.value.status_code == 409
    dispatch.assert_not_called()
    assert (
        deployment_commands.start_rollback(target_deployment_id=target.id, **kwargs).id
        == retry.id
    )
    dispatch.assert_called_once()


def test_reconciler_cannot_stage_or_submit_a_new_planner_build_while_paused(
    paused, monkeypatch
):
    from eng_platform_api.services import release_reconciler

    execution = {
        "execution_id": "legacy",
        "provider": "cloud_build",
        "build_id": "existing",
        "status": "failed",
    }
    inspect, stage, submit = Mock(), Mock(), Mock()
    monkeypatch.setattr(release_cloud_build, "get_build", inspect)
    monkeypatch.setattr(release_executions, "stage_planner_retry", stage)
    monkeypatch.setattr(release_cloud_build, "submit", submit)
    assert (
        release_reconciler._retry_planner_after_verified_step_failure(execution)
        is execution
    )
    inspect.assert_not_called()
    stage.assert_not_called()
    submit.assert_not_called()


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_paused_webhook_cannot_reserve_cloud_build_or_poison_later_github_choice(
    paused, monkeypatch, event
):
    from eng_platform_api.services import release_orchestrator

    paused.release.executor = "cloud_build"
    settings = config.release_orchestrator
    monkeypatch.setattr(settings, "enabled", True)
    monkeypatch.setattr(settings, "enabled_services", (paused.service_name,))
    monkeypatch.setattr(settings, "quality_python_image", "quality@sha256:" + "d" * 64)
    monkeypatch.setattr(settings, "release_planner_image", "planner@sha256:" + "e" * 64)
    monkeypatch.setattr(release_orchestrator, "_services", lambda _: [paused])
    monkeypatch.setattr(catalog, "get_service", lambda _: paused)
    monkeypatch.setattr(
        release_orchestrator.github_release_control, "is_fast_forward", lambda *_: True
    )
    session = Mock()
    monkeypatch.setattr(release_cloud_build, "_session", session)
    payload = {
        "repository": {"full_name": paused.repository},
        "ref": "refs/heads/main",
        "before": "b" * 40,
        "after": "a" * 40,
        "number": 1,
        "action": "opened",
        "pull_request": {
            "head": {"sha": "a" * 40, "ref": "feature"},
            "base": {"sha": "b" * 40},
        },
    }
    handler = (
        release_orchestrator.handle_push
        if event == "push"
        else release_orchestrator.handle_pull_request
    )
    with pytest.raises(HTTPException) as error:
        handler(payload, delivery_id="blocked")
    assert error.value.status_code == 409
    assert not release_executions._memory
    session.assert_not_called()
    if event == "push":
        execution = release_orchestrator.start_release(
            service_name=paused.service_name,
            head_sha="a" * 40,
            base_sha="b" * 40,
            requested_by="diegomad14",
            provider="github_actions",
        )
    else:
        execution, created = release_orchestrator._reserve(
            service=paused,
            operation="pr_quality",
            head_sha="a" * 40,
            base_sha="b" * 40,
            branch="feature",
            delivery_id="github",
            provider="github_actions",
            selection=ProviderSelection(
                requested="github_actions",
                service_default="cloud_build",
                effective="github_actions",
                source="request",
            ),
        )
        assert created
    assert execution["provider"] == "github_actions"
    assert execution["provider_selection"]["requested"] == "github_actions"
    session.assert_not_called()


@pytest.mark.parametrize("contents", ["{broken", "{}", "null"])
def test_corrupt_durable_intent_does_not_become_an_empty_store(
    paused, monkeypatch, tmp_path, contents
):
    from eng_platform_api.services import deployment_store

    path = tmp_path / "requests.json"
    path.write_text(contents)
    monkeypatch.setattr(deployment_store, "_DEFAULT_STORE_PATH", path)
    monkeypatch.setattr(deployment_store, "_COLLECTION", "")
    dispatch = Mock()
    monkeypatch.setattr(github_deployments, "start_deployment", dispatch)
    with pytest.raises(HTTPException) as error:
        deployment_commands.start_deployment(
            service_name=paused.service_name,
            tag_name="v1",
            provider="github_actions",
            requested_by="diegomad14",
            idempotency_key="same",
        )
    assert error.value.status_code == 503
    dispatch.assert_not_called()


@pytest.mark.parametrize("other", [None, "cloud_build"])
def test_one_repository_has_one_release_default_but_independent_deploy_defaults(other):
    from copy import deepcopy
    from eng_platform_api.services import log_catalog

    first = deepcopy(
        next(
            row
            for row in log_catalog.load_catalog()
            if row["management_mode"] == "managed"
        )
    )
    second = deepcopy(first)
    second["service_name"] = "another-managed-service"
    first["release"] = {"executor": "github_actions"}
    second["release"] = {"executor": other}
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.validate_catalog({"services": [first, second]})
    second["release"] = first["release"]
    first["deployment"]["executor"] = "github_actions"
    second["deployment"]["executor"] = "cloud_build"
    assert len(log_catalog.validate_catalog({"services": [first, second]})) == 2
