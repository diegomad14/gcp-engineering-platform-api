"""Artemis context is catalog-owned, signed, durable and workflow-compatible."""

import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from eng_platform_api.config import config, load_config
from eng_platform_api.models import DeploymentItem, ProviderSelection, ReleaseTag
from eng_platform_api.services import (
    catalog,
    cloud_build,
    deployment_executions,
    deployment_store,
    github_deployments,
    github_execution_config,
    log_catalog,
    provider_selection,
    release_authorization,
)

# Collection happens before the mock-mode fixture. Parametrize from the
# synthetic catalog explicitly, without selecting a private runtime source.
ARTEMIS_NAMES = [
    row["service_name"]
    for row in log_catalog.load_catalog(log_catalog.CATALOG_PATH)
    if row["service_name"].startswith("cgm-artemis-")
]


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(config.github, "deployment_evidence_bucket", "offline-evidence")
    monkeypatch.setattr(config.cloud_build, "evidence_bucket", "offline-evidence")
    monkeypatch.setattr(config.release_orchestrator, "artemis_web_sha", "b" * 40)
    monkeypatch.setattr(deployment_executions, "_collection", lambda: None)
    monkeypatch.setattr(deployment_executions, "_memory", {})
    monkeypatch.setattr(deployment_store, "_COLLECTION", "")
    monkeypatch.setattr(
        deployment_store, "_DEFAULT_STORE_PATH", tmp_path / "store.json"
    )
    key = (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    monkeypatch.setattr(config.github, "release_signing_private_key", key)
    monkeypatch.setattr(config.github, "release_signing_public_key", "")
    client, repo, workflow = Mock(), Mock(), Mock()
    client.get_repo.return_value = repo
    repo.get_workflow.return_value = workflow
    repo.create_deployment.return_value.id = 101
    workflow.create_dispatch.return_value = True
    monkeypatch.setattr(github_deployments, "github_client", lambda: client)
    return repo, workflow


def item(service, **values):
    return DeploymentItem(
        id="existing",
        service_name=service.service_name,
        repository=service.repository,
        tag="v1.0.0",
        sha="a" * 40,
        **values,
    )


@pytest.mark.parametrize("service_name", ARTEMIS_NAMES)
@pytest.mark.parametrize("kind", ["deploy", "rollback"])
def test_signed_dispatch_matches_workflow_and_cloud_engine_context(
    configured, monkeypatch, service_name, kind
):
    service = catalog.get_service(service_name)
    monkeypatch.setattr(log_catalog, "load_catalog", lambda: [service.model_dump()])
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.cloud_build, "enabled", False)
    monkeypatch.setattr(config.cloud_build, "new_builds_enabled", False)
    selected = ProviderSelection(
        requested="github_actions",
        service_default=service.deployment.executor
        if service.deployment.executor != "auto"
        else None,
        effective="github_actions",
        source="request",
    )
    with (
        provider_selection.using(selected),
        deployment_store.using_command_key("stable"),
    ):
        if kind == "deploy":
            result = github_deployments.start_deployment(
                service=service,
                tag=ReleaseTag(name="v1.0.0", sha="a" * 40),
                requested_by="diegomad14",
            )
        else:
            result = github_deployments.start_rollback(
                service=service,
                target=item(
                    service, status="SUCCEEDED", production_revision="original-revision"
                ),
                requested_by="diegomad14",
            )
    repo, workflow = configured
    workflow.create_dispatch.assert_called_once()
    inputs = workflow.create_dispatch.call_args.kwargs["inputs"]
    filename = "platform-rollback.yml" if kind == "rollback" else "platform-deploy.yml"
    document = yaml.load(
        (Path(__file__).parents[1] / ".github/workflows" / filename).read_text(),
        Loader=yaml.BaseLoader,
    )
    assert set(inputs) == set(document["on"]["workflow_dispatch"]["inputs"])
    context = json.loads(inputs["execution_config"])
    digest = hashlib.sha256(inputs["execution_config"].encode()).hexdigest()
    claims = release_authorization.verify(
        inputs["platform_authorization"],
        {
            "repository": service.repository,
            "service_name": service_name,
            "sha": result.sha,
            "kind": kind,
            "configuration_hash": digest,
        },
    )
    assert claims["configuration_hash"] == digest
    assert claims["workflow_continuation"] is True
    assert (
        deployment_executions.get(result.id)["github_execution_config"]
        == inputs["execution_config"]
    )
    assert deployment_executions.get(result.id)["status"] == "GITHUB_ACCEPTED"
    assert deployment_store.find_by_idempotency_key("stable").id == result.id
    monkeypatch.setattr(cloud_build, "_require_config", lambda: None)
    monkeypatch.setattr(
        cloud_build, "_repository", lambda _: "synthetic-connected-repo"
    )
    env = dict(
        row.split("=", 1)
        for row in cloud_build.build_request(result, service)["steps"][0]["env"]
    )
    assert env["CGM_RUNTIME_KIND"] == context["runtime_kind"]
    assert env["CGM_EVIDENCE_BUCKET"] == context["evidence_bucket"]
    assert env["CGM_PRIVATE_RUNTIME"] == str(context["private_runtime"]).lower()
    assert env["CGM_DOCKERFILE_PATH"] == context["dockerfile_path"]
    assert env["CGM_QUALITY_EVIDENCE_SERVICES"] == ",".join(
        context["quality_evidence_services"]
    )
    assert env["CGM_REPOSITORY_ALIASES"] == ",".join(context["repository_aliases"])
    assert env["CGM_PAIRED_WEB_SHA"] == context["paired_web_sha"]
    repo.create_deployment.assert_called_once()


def test_retry_reuses_first_signed_context_after_configuration_changes(
    configured, monkeypatch
):
    service = catalog.get_service("cgm-artemis-job-worker")
    value = item(
        service,
        github_deployment_id=73,
        status="FAILED",
        current_stage="dispatch",
        provider_selection=ProviderSelection(
            effective="github_actions", source="legacy_execution"
        ),
    )
    original = github_execution_config.execution_config_for(service, value)
    kwargs = dict(
        provider="github_actions",
        fingerprint="original",
        service_name=value.service_name,
        repository=value.repository,
        sha=value.sha,
        tag=value.tag,
        kind=value.kind,
    )
    deployment_executions.reserve(value.id, **kwargs)
    github_deployments._engine_inputs(service, value)
    assert deployment_executions.get(value.id)["github_execution_config"] == original
    monkeypatch.setattr(config.github, "deployment_evidence_bucket", "changed-bucket")
    monkeypatch.setattr(config.release_orchestrator, "artemis_web_sha", "c" * 40)
    service.deployment.dockerfile_path = "changed.Dockerfile"
    result = github_deployments.retry_dispatch(service=service, item=value)
    inputs = configured[1].create_dispatch.call_args.kwargs["inputs"]
    assert result.id == value.id and inputs["execution_config"] == original
    claims = release_authorization.verify(inputs["platform_authorization"], {})
    assert claims["configuration_hash"] == hashlib.sha256(original.encode()).hexdigest()
    with pytest.raises(ValueError, match="configuration cannot change"):
        deployment_executions.reserve(
            value.id, **kwargs, github_execution_config="different"
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("evidence_bucket", ""),
        ("runtime_kind", []),
        ("private_runtime", "false"),
        ("dockerfile_path", "../Dockerfile"),
        ("dockerfile_path", "/Dockerfile"),
        ("paired_web_sha", ""),
        ("paired_web_sha", "wrong"),
        ("quality_evidence_services", "cgm-artemis-api"),
        ("quality_evidence_services", ["service\nINJECT=x"]),
        ("repository_aliases", ["invalid"]),
        ("repository_aliases", [7]),
    ],
)
def test_corrupt_persisted_context_stops_before_work(configured, field, value):
    service = catalog.get_service("cgm-artemis-api")
    target = item(service)
    payload = json.loads(github_execution_config.execution_config_for(service, target))
    payload[field] = value
    deployment_executions.reserve(
        target.id,
        provider="github_actions",
        fingerprint="f",
        service_name=target.service_name,
        repository=target.repository,
        sha=target.sha,
        tag=target.tag,
        kind=target.kind,
        github_execution_config=json.dumps(payload),
    )
    with pytest.raises(ValueError):
        github_execution_config.execution_config_for(service, target)
    configured[1].create_dispatch.assert_not_called()


def test_non_artemis_keeps_legacy_workflow_inputs(configured):
    service = catalog.get_service("eng-platform-api")
    assert github_deployments._engine_inputs(service, item(service)) == {}


@pytest.mark.parametrize(
    "deployment,quality,legacy,expected",
    [
        ("specific", "quality", "legacy", "specific"),
        ("", "quality", "legacy", "quality"),
        ("", "", "legacy", "legacy"),
        ("", "", "", ""),
    ],
)
def test_evidence_bucket_configuration_is_independent_of_cloud_activation(
    monkeypatch, deployment, quality, legacy, expected
):
    for name, value in {
        "ENG_PLATFORM_DEPLOYMENT_EVIDENCE_BUCKET": deployment,
        "ENG_PLATFORM_QUALITY_BUCKET": quality,
        "ENG_PLATFORM_CLOUD_BUILD_EVIDENCE_BUCKET": legacy,
        "ENG_PLATFORM_CLOUD_BUILD_ENABLED": "false",
    }.items():
        monkeypatch.setenv(name, value)
    assert load_config().github.deployment_evidence_bucket == expected
