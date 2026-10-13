"""Retry admission survives failed lookups without reopening uncertain dispatches."""

from unittest.mock import Mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from eng_platform_api.config import config
from eng_platform_api.models import DeploymentItem, ProviderSelection
from eng_platform_api.services import (
    catalog,
    deployment_executions,
    deployment_store,
    github_deployments,
    release_authorization,
)


@pytest.fixture
def retry_case(monkeypatch, tmp_path):
    service = catalog.get_service("eng-platform-api")
    assert service is not None
    # A later service default must not replace the original GitHub intention.
    service.deployment.executor = "cloud_build"
    monkeypatch.setattr(config.cloud_build, "enabled", False)
    monkeypatch.setattr(config.cloud_build, "new_builds_enabled", False)
    monkeypatch.setattr(deployment_executions, "_collection", lambda: None)
    monkeypatch.setattr(deployment_executions, "_memory", {})
    monkeypatch.setattr(deployment_store, "_COLLECTION", "")
    monkeypatch.setattr(
        deployment_store, "_DEFAULT_STORE_PATH", tmp_path / "deployments.json"
    )
    private_key = (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    monkeypatch.setattr(config.github, "release_signing_private_key", private_key)
    monkeypatch.setattr(config.github, "release_signing_public_key", "")
    authorize = Mock(wraps=release_authorization.issue)
    monkeypatch.setattr(release_authorization, "issue", authorize)
    client, repository, deployment, workflow = (Mock() for _ in range(4))
    client.get_repo.return_value = repository
    repository.get_deployment.return_value = deployment
    repository.get_workflow.return_value = workflow
    workflow.create_dispatch.return_value = True
    monkeypatch.setattr(github_deployments, "github_client", lambda: client)
    return service, client, repository, deployment, workflow, authorize


def _reserve_failed_item(service, kind, admission_status):
    value = DeploymentItem(
        id="73",
        github_deployment_id=73,
        service_name=service.service_name,
        repository=service.repository,
        tag="v0.5.0",
        sha="a" * 40,
        kind=kind,
        status="FAILED",
        current_stage="dispatch",
        error=github_deployments.GITHUB_WORKFLOW_DISPATCH_FAILED,
        requested_by="diegomad14",
        production_revision="eng-platform-api-00010-abc" if kind == "rollback" else "",
        rollback_target_deployment_id="original-target" if kind == "rollback" else "",
        provider_selection=ProviderSelection(
            requested="github_actions",
            service_default="cloud_build",
            effective="github_actions",
            source="request",
        ),
    )
    deployment_executions.reserve(
        value.id,
        provider="github_actions",
        fingerprint="f" * 64,
        service_name=value.service_name,
        repository=value.repository,
        sha=value.sha,
        tag=value.tag,
        kind=value.kind,
        authorization_jti="original-ticket",
        provider_selection=value.provider_selection.model_dump(),
    )
    deployment_executions.save(value.id, status=admission_status)
    deployment_store.save(value, "original-idempotency-token")
    return value


@pytest.mark.parametrize("lookup", ["get_repo", "get_deployment"])
@pytest.mark.parametrize("kind", ["deploy", "rollback"])
@pytest.mark.parametrize("admission_status", ["GITHUB_REJECTED", "SUBMISSION_PENDING"])
def test_failed_lookup_restores_original_admission_then_dispatches_once(
    retry_case, lookup, kind, admission_status
):
    service, client, repository, deployment, workflow, authorize = retry_case
    value = _reserve_failed_item(service, kind, admission_status)
    original = deployment_executions.get(value.id)
    identity = value.model_dump()
    target = client.get_repo if lookup == "get_repo" else repository.get_deployment
    response = repository if lookup == "get_repo" else deployment

    def fail_after_claim(*_):
        assert deployment_executions.get(value.id)["status"] == "GITHUB_DISPATCHING"
        raise RuntimeError("simulated read failure before any dispatch")

    target.side_effect = fail_after_claim
    with pytest.raises(github_deployments.GitHubDispatchError) as raised:
        github_deployments.retry_dispatch(service=service, item=value)

    assert raised.value.dispatch_attempted is False
    assert raised.value.dispatch_error_detail == ""
    assert raised.value.item is value
    restored = deployment_executions.get(value.id)
    assert {key: v for key, v in restored.items() if key != "updated_at"} == {
        key: v for key, v in original.items() if key != "updated_at"
    }
    assert value.id == identity["id"]
    assert value.github_deployment_id == identity["github_deployment_id"]
    assert value.provider_selection.model_dump() == identity["provider_selection"]
    assert (
        deployment_store.idempotency_key_for(value.id) == "original-idempotency-token"
    )
    assert (
        deployment_store.find_by_idempotency_key("original-idempotency-token").id
        == "73"
    )
    workflow.create_dispatch.assert_not_called()
    authorize.assert_not_called()
    deployment.create_status.assert_not_called()

    target.side_effect = None
    target.return_value = response
    retried = github_deployments.retry_dispatch(service=service, item=value)

    assert retried.id == "73" and retried.github_deployment_id == 73
    assert retried.sha == identity["sha"] and retried.tag == identity["tag"]
    assert retried.production_revision == identity["production_revision"]
    assert (
        retried.rollback_target_deployment_id
        == identity["rollback_target_deployment_id"]
    )
    assert retried.provider_selection.model_dump() == identity["provider_selection"]
    assert retried.status == "QUEUED" and retried.error == ""
    assert (
        deployment_store.idempotency_key_for(value.id) == "original-idempotency-token"
    )
    workflow.create_dispatch.assert_called_once()
    repository.create_deployment.assert_not_called()
    authorize.assert_called_once()
    call = workflow.create_dispatch.call_args.kwargs
    assert call["ref"] == ("main" if kind == "rollback" else identity["tag"])
    claims = release_authorization.verify(
        call["inputs"]["platform_authorization"],
        {
            "repository": value.repository,
            "service_name": value.service_name,
            "github_deployment_id": "73",
            "sha": identity["sha"],
            "kind": kind,
        },
    )
    accepted = deployment_executions.get(value.id)
    assert accepted["status"] == "GITHUB_ACCEPTED"
    assert accepted["provider"] == "github_actions"
    assert accepted["authorization_jti"] == claims["jti"]
    assert accepted["fingerprint"] == original["fingerprint"]
    assert accepted["provider_selection"] == original["provider_selection"]


@pytest.mark.parametrize("kind", ["deploy", "rollback"])
def test_uncertain_attempt_keeps_admission_closed_and_cannot_dispatch_again(
    retry_case, kind
):
    service, client, repository, _, workflow, authorize = retry_case
    value = _reserve_failed_item(service, kind, "GITHUB_REJECTED")
    workflow.create_dispatch.side_effect = RuntimeError(
        "simulated lost dispatch response"
    )

    with pytest.raises(github_deployments.GitHubDispatchError) as raised:
        github_deployments.retry_dispatch(service=service, item=value)

    assert raised.value.dispatch_attempted is True
    record = deployment_executions.get(value.id)
    assert record["status"] == "GITHUB_UNCERTAIN"
    assert record["provider"] == "github_actions"
    assert record["provider_selection"] == value.provider_selection.model_dump()
    assert record["authorization_jti"] != "original-ticket"
    assert (
        deployment_store.idempotency_key_for(value.id) == "original-idempotency-token"
    )

    with pytest.raises(ValueError, match="pending or uncertain"):
        github_deployments.retry_dispatch(service=service, item=value)

    assert deployment_executions.get(value.id) == record
    client.get_repo.assert_called_once_with(value.repository)
    repository.get_deployment.assert_called_once_with(73)
    repository.create_deployment.assert_not_called()
    workflow.create_dispatch.assert_called_once()
    authorize.assert_called_once()
