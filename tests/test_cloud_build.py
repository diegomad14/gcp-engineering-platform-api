"""Economic, immutable Cloud Build request and idempotency contracts."""

from unittest import mock
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from eng_platform_api.config import config
from eng_platform_api.models import DeploymentItem
from eng_platform_api.services import catalog, cloud_build, deployment_executions
from eng_platform_api.services.release_profiles import profile_for


@pytest.fixture(autouse=True)
def cloud_build_settings(monkeypatch):
    monkeypatch.setattr(config.cloud_build, "enabled", True)
    monkeypatch.setattr(config.cloud_build, "mode", "auto")
    monkeypatch.setattr(config.cloud_build, "project_id", "cgm-assistant-prod")
    monkeypatch.setattr(
        config.cloud_build,
        "service_account",
        "projects/cgm-assistant-prod/serviceAccounts/release@example.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(
        config.cloud_build,
        "executor_image",
        "us-central1-docker.pkg.dev/project/repo/executor@sha256:" + "a" * 64,
    )
    monkeypatch.setattr(
        config.cloud_build,
        "repositories",
        {
            "eng-platform-api": (
                "projects/cgm-assistant-prod/locations/us-central1/"
                "connections/github/repositories/eng-platform-api"
            )
        },
    )
    deployment_executions._memory.clear()


def _item() -> DeploymentItem:
    return DeploymentItem(
        id="123",
        service_name="eng-platform-api",
        repository="diegomad14/gcp-engineering-platform-api",
        tag="v1.2.3",
        sha="b" * 40,
    )


def test_build_request_has_fixed_economy_contract():
    service = catalog.get_service("eng-platform-api")
    assert service is not None
    request = cloud_build.build_request(_item(), service)

    assert request["options"] == {
        "machineType": "E2_STANDARD_2",
        "logging": "CLOUD_LOGGING_ONLY",
    }
    assert request["timeout"] == "1800s"
    assert len(request["steps"]) == 1
    assert "@sha256:" in request["steps"][0]["name"]
    assert request["source"]["connectedRepository"]["revision"] == "b" * 40

    serialized = str(request).lower()
    for forbidden in (
        "pytest",
        "coverage",
        "scanner",
        "sonar",
        "postgres",
        "semantic-release",
        "privatepool",
        "disk_size",
    ):
        assert forbidden not in serialized


def test_executor_image_must_be_immutable(monkeypatch):
    monkeypatch.setattr(config.cloud_build, "executor_image", "example/executor:latest")
    service = catalog.get_service("eng-platform-api")
    assert service is not None
    with pytest.raises(cloud_build.CloudBuildError, match="pinned by digest"):
        cloud_build.build_request(_item(), service)


def test_submit_reuses_matching_build_instead_of_posting_twice(monkeypatch):
    service = catalog.get_service("eng-platform-api")
    assert service is not None
    item = _item()
    expected_fingerprint = cloud_build.fingerprint(item, service)
    existing = {
        "id": "build-1",
        "status": "QUEUED",
        "logUrl": "https://example.invalid/build-1",
        "substitutions": {
            "_REQUEST_FINGERPRINT": expected_fingerprint,
            "_DEPLOYMENT_ID": item.id,
            "_RELEASE_SHA": item.sha,
        },
    }
    session = mock.MagicMock()
    monkeypatch.setattr(cloud_build, "_matching_build", lambda *_: existing)
    monkeypatch.setattr(cloud_build, "_session", lambda: session)

    result = cloud_build.submit(item, service, reason="quota")

    assert result.logs_url == existing["logUrl"]
    session.post.assert_not_called()
    assert deployment_executions.get(item.id)["build_id"] == "build-1"


def test_disabled_cloud_build_never_builds_a_request(monkeypatch):
    monkeypatch.setattr(config.cloud_build, "enabled", False)
    service = catalog.get_service("eng-platform-api")
    assert service is not None
    with pytest.raises(cloud_build.CloudBuildError, match="disabled"):
        cloud_build.build_request(_item(), service)


def test_executor_and_backend_authorize_the_same_release_spec():
    path = Path(__file__).parents[1] / "docker/release-executor/release_executor.py"
    spec = spec_from_file_location("release_executor_contract", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    for service_name in module.PROFILE_SPECS:
        service = catalog.get_service(service_name)
        assert service is not None
        assert (
            module.profile_fingerprint(service_name)
            == profile_for(service).fingerprint()
        )
