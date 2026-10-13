"""Offline composition of candidate enrollment, startup and Artemis selection."""

import json
import os
from unittest import mock

import pytest
from fastapi import HTTPException

from eng_platform_api import verify_candidate_config as candidate
from eng_platform_api.config import config, load_config
from eng_platform_api.models import DeploymentItem, ReleaseTag
from eng_platform_api.services import (
    catalog,
    cloud_build_policy,
    deployment_commands as commands,
    provider_selection,
)

REVISION = "offline-candidate"
ARTEMIS_SERVICES = candidate.BASELINE_CLOUD_BUILD_ONLY_SERVICES


def _environment(enrolled: set[str], *, cloud_build_enabled: bool) -> dict[str, str]:
    image = "offline.invalid/executor@sha256:" + "a" * 64
    return {
        "ENG_PLATFORM_MOCK_MODE": "true",
        "ENG_PLATFORM_PRIVATE_EXECUTOR_MODE": "github_actions",
        "ENG_PLATFORM_RELEASE_ORCHESTRATOR_ENABLED": "true",
        "ENG_PLATFORM_GITHUB_WEBHOOK_SECRET": "offline-fixture",
        "ENG_PLATFORM_GITHUB_APP_ID": "1",
        "ENG_PLATFORM_GITHUB_INSTALLATION_ID": "1",
        "ENG_PLATFORM_GITHUB_PRIVATE_KEY": "offline-fixture",
        "ENG_PLATFORM_API_URL": "http://localhost:8000",
        "ENG_PLATFORM_RELEASE_RECONCILER_SERVICE_ACCOUNT": "reconciler@offline.iam.gserviceaccount.com",
        "ENG_PLATFORM_RELEASE_QUALITY_SERVICE_ACCOUNT": "quality@offline.iam.gserviceaccount.com",
        "ENG_PLATFORM_RELEASE_CALLBACK_SERVICE_ACCOUNT": "quality@offline.iam.gserviceaccount.com",
        "ENG_PLATFORM_QUALITY_BUCKET": "offline-fixture",
        "ENG_PLATFORM_CLOUD_BUILD_ENABLED": str(cloud_build_enabled).lower(),
        "ENG_PLATFORM_CLOUD_BUILD_PROJECT_ID": "offline-project",
        "ENG_PLATFORM_CLOUD_BUILD_SERVICE_ACCOUNT": "deploy@offline.iam.gserviceaccount.com",
        "ENG_PLATFORM_CLOUD_BUILD_CALLBACK_SERVICE_ACCOUNT": "deploy@offline.iam.gserviceaccount.com",
        "ENG_PLATFORM_CLOUD_BUILD_EXECUTOR_IMAGE": image,
        "ENG_PLATFORM_CLOUD_BUILD_EVIDENCE_BUCKET": "offline-fixture",
        candidate.WRITER_ENV: candidate.EXPECTED_WRITER,
        candidate.ENABLED_SERVICES_ENV: ",".join(sorted(enrolled)),
        **candidate._tooling_images(),
    }


def _verified(environment: dict[str, str]) -> bool:
    revision = {
        "metadata": {"name": REVISION},
        "spec": {
            "containers": [
                {
                    "env": [
                        {"name": name, "value": value}
                        for name, value in environment.items()
                    ]
                }
            ]
        },
    }
    # Candidate verification's sole cloud read is replaced with a local response.
    with mock.patch.object(candidate.subprocess, "run") as query:
        query.return_value.stdout = json.dumps(revision)
        return candidate.verify("offline-project", "offline-region", REVISION)


def _assert_github_admission_blocked(monkeypatch, tmp_path, service_name, detail):
    monkeypatch.setattr(commands.deployment_store, "_COLLECTION", "")
    monkeypatch.setattr(
        commands.deployment_store, "_DEFAULT_STORE_PATH", tmp_path / "deployments.json"
    )
    with (
        mock.patch.object(commands.deployment_store, "save") as save,
        mock.patch.object(commands.github_deployments, "get_tag") as get_tag,
        mock.patch.object(commands.github_deployments, "start_deployment") as github,
        mock.patch.object(commands.cloud_build, "submit") as cloud,
        pytest.raises(HTTPException, match=detail) as denied,
    ):
        commands.start_deployment(
            service_name=service_name,
            tag_name="v1.2.3",
            requested_by="offline-operator",
            idempotency_key="offline-idempotency",
            provider="github_actions",
        )
    assert denied.value.status_code == 409
    save.assert_not_called()
    get_tag.assert_not_called()
    github.assert_not_called()
    cloud.assert_not_called()


@pytest.mark.parametrize("cloud_build_enabled", [False, True])
@pytest.mark.parametrize("platform_enrolled", [False, True])
def test_complete_enrollment_composes_with_github_startup_and_paused_builds(
    monkeypatch, cloud_build_enabled, platform_enrolled
):
    enrolled = set(candidate.CLOUD_BUILD_ONLY_SERVICES)
    if not platform_enrolled:
        enrolled -= candidate.GITHUB_ONLY_PLATFORM_SERVICES
    environment = _environment(enrolled, cloud_build_enabled=cloud_build_enabled)
    environment[candidate.CLOUD_BUILD_ONLY_ENV] = ",".join(sorted(enrolled))

    assert _verified(environment)
    with mock.patch.dict(os.environ, environment, clear=True):
        loaded = load_config()

    assert set(loaded.cloud_build.enabled_services) == enrolled
    assert set(loaded.cloud_build.cloud_build_only_services) == enrolled
    assert loaded.release_orchestrator.private_executor_mode == "github_actions"
    assert loaded.release_orchestrator.enabled
    assert not loaded.cloud_build.new_builds_enabled
    if not platform_enrolled:
        assert candidate.GITHUB_ONLY_PLATFORM_SERVICES.isdisjoint(
            loaded.cloud_build.enabled_services
        )
    monkeypatch.setattr(cloud_build_policy, "config", loaded)
    with pytest.raises(HTTPException, match="New Cloud Builds are paused") as denied:
        cloud_build_policy.require_new_builds()
    assert denied.value.status_code == 409


@pytest.mark.parametrize(
    "enrolled", [None, "", "cgm-artemis-api", "eng-platform-api", "eng-platform-web"]
)
def test_candidate_routing_check_does_not_certify_startup_for_partial_enrollment(
    enrolled,
):
    """PR157's routing checker accepts partial fixtures that startup rejects.

    Preserve the startup safety invariant: this documents a verifier limitation,
    and must not be mistaken for a runnable candidate or used to enable fallback.
    """
    environment = _environment(set(), cloud_build_enabled=False)
    if enrolled is None:
        environment.pop(candidate.ENABLED_SERVICES_ENV)
    else:
        environment[candidate.ENABLED_SERVICES_ENV] = enrolled
    rows = [{"name": name, "value": value} for name, value in environment.items()]
    environment[candidate.CLOUD_BUILD_ONLY_ENV] = ",".join(
        sorted(candidate._expected_routing(rows))
    )

    assert _verified(environment)
    with (
        mock.patch.dict(os.environ, environment, clear=True),
        pytest.raises(ValueError, match="Cloud Build-only services must be enabled"),
    ):
        load_config()


@pytest.mark.parametrize("service_name", ARTEMIS_SERVICES)
def test_artemis_explicit_github_uses_github_readiness_without_changing_defaults(
    monkeypatch, service_name
):
    # Load the checked-in catalog and the actual selector. No provider lookup or
    # remote probe is needed to select an explicit provider.
    monkeypatch.setattr(config, "catalog_path", None)
    monkeypatch.setattr(config.github, "deployment_evidence_bucket", "offline-evidence")
    monkeypatch.setattr(config.release_orchestrator, "artemis_web_sha", "b" * 40)
    monkeypatch.setattr(config.cloud_build, "enabled", False)
    monkeypatch.setattr(config.cloud_build, "new_builds_enabled", False)
    original = catalog.get_service(service_name)
    assert original is not None
    before = original.model_dump()
    assert original.deployment.executor == (
        "auto" if service_name == "cgm-artemis-web" else "cloud_build"
    )
    assert original.release.executor is None
    assert original.deployment_provider_readiness["github_actions"].ready
    assert not original.deployment_provider_readiness["cloud_build"].ready

    with mock.patch.object(
        commands.github_actions_quota,
        "should_use_cloud_build",
        side_effect=AssertionError("Explicit GitHub must not query legacy fallback"),
    ) as legacy_lookup:
        selected = commands._selection(original, "github_actions")
    legacy_lookup.assert_not_called()
    assert selected.effective == "github_actions"
    assert selected.requested == "github_actions"
    assert selected.service_default == (
        None if service_name == "cgm-artemis-web" else "cloud_build"
    )
    assert selected.source == "request" and selected.locked
    # A deployment default never becomes a release default implicitly.
    release = provider_selection.select(
        original, None, operation="release", legacy_provider="github_actions"
    )
    assert release.effective == "github_actions"
    assert release.service_default is None and release.source == "legacy_policy"

    with mock.patch.object(
        catalog, "deployment_blockers", wraps=catalog.deployment_blockers
    ) as blockers:
        ready = commands._service_or_404(service_name, selected.effective)
    assert ready.deployment_ready
    assert blockers.call_args.args[0].deployment.executor == "github_actions"
    # The readiness copy must retain the catalog's default for intent metadata.
    assert ready.deployment.executor == original.deployment.executor

    item = DeploymentItem(
        id="offline-" + service_name,
        service_name=service_name,
        repository=original.repository,
        tag="v1.2.3",
        sha="a" * 40,
    )
    with (
        mock.patch.object(
            commands.github_deployments, "start_deployment", return_value=item
        ) as github_transport,
        mock.patch.object(commands, "start_cloud_build") as cloud_transport,
    ):
        dispatched = commands._dispatch_deploy(
            ready,
            ReleaseTag(name=item.tag, sha=item.sha),
            "offline-operator",
            "github_actions",
        )
    github_transport.assert_called_once()
    cloud_transport.assert_not_called()
    assert dispatched.provider_selection == selected
    assert catalog.get_service(service_name).model_dump() == before


@pytest.mark.parametrize("service_name", ARTEMIS_SERVICES)
@pytest.mark.parametrize("bucket", ["", "bad bucket", "gs://bucket", "bucket\n"])
def test_artemis_github_readiness_rejects_missing_or_invalid_evidence_bucket(
    monkeypatch, tmp_path, service_name, bucket
):
    monkeypatch.setattr(config, "catalog_path", None)
    monkeypatch.setattr(config.github, "deployment_evidence_bucket", bucket)
    monkeypatch.setattr(config.release_orchestrator, "artemis_web_sha", "b" * 40)
    service = catalog.get_service(service_name)
    readiness = service.deployment_provider_readiness["github_actions"]
    assert not readiness.ready
    assert any("evidence bucket" in blocker for blocker in readiness.blockers)
    with pytest.raises(HTTPException, match="evidence bucket") as denied:
        commands._service_or_404(service_name, "github_actions")
    assert denied.value.status_code == 409
    _assert_github_admission_blocked(
        monkeypatch, tmp_path, service_name, "evidence bucket"
    )


@pytest.mark.parametrize("paired_sha", ["", "not-a-sha", "a" * 39, "A" * 40])
def test_artemis_api_github_readiness_requires_pinned_paired_web_sha(
    monkeypatch, tmp_path, paired_sha
):
    monkeypatch.setattr(config, "catalog_path", None)
    monkeypatch.setattr(config.github, "deployment_evidence_bucket", "offline-evidence")
    monkeypatch.setattr(config.release_orchestrator, "artemis_web_sha", paired_sha)
    service = catalog.get_service("cgm-artemis-api")
    readiness = service.deployment_provider_readiness["github_actions"]
    assert not readiness.ready
    assert any("paired web SHA" in blocker for blocker in readiness.blockers)
    with pytest.raises(HTTPException, match="paired web SHA") as denied:
        commands._service_or_404("cgm-artemis-api", "github_actions")
    assert denied.value.status_code == 409
    _assert_github_admission_blocked(
        monkeypatch, tmp_path, "cgm-artemis-api", "paired web SHA"
    )


@pytest.mark.parametrize(
    "service_name", [name for name in ARTEMIS_SERVICES if name != "cgm-artemis-api"]
)
def test_artemis_workers_and_web_do_not_require_paired_web_sha(
    monkeypatch, service_name
):
    monkeypatch.setattr(config, "catalog_path", None)
    monkeypatch.setattr(config.github, "deployment_evidence_bucket", "offline-evidence")
    monkeypatch.setattr(config.release_orchestrator, "artemis_web_sha", "")
    service = catalog.get_service(service_name)
    assert service.deployment_provider_readiness["github_actions"].ready
    assert commands._service_or_404(service_name, "github_actions").deployment_ready
