"""Cloud Build transport tests that never contact Google Cloud."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from unittest import mock

import pytest

from eng_platform_api.models import CatalogService, ServiceQualityConfig
from eng_platform_api.services import release_cloud_build as cloud_build


HEAD = "a" * 40
BASE = "b" * 40
DIGEST = "quality@sha256:" + "c" * 64
PLANNER = "planner@sha256:" + "d" * 64
POSTGRES = "postgres@sha256:" + "e" * 64
PLANNER_HASH = "1" * 64
REPOSITORY_RESOURCE = (
    "projects/test-project/locations/us-central1/"
    "connections/github/repositories/eng-platform-web"
)


@dataclass
class Response:
    status_code: int = 200
    payload: dict | None = None

    def json(self):
        return self.payload or {}


def _service(
    *,
    name: str = "eng-platform-web",
    repository: str = "owner/eng-platform-web",
    runtime: str = "node",
    coverage: int = 80,
) -> CatalogService:
    return CatalogService(
        service_name=name,
        repository=repository,
        owner="platform",
        project_id="test-project",
        region="us-central1",
        quality=ServiceQualityConfig(
            enabled=True, profile=runtime, coverage_threshold=coverage
        ),
    )


def _execution(*, operation: str = "pr_quality", **overrides) -> dict:
    service = _service()
    value = {
        "execution_id": "f" * 64,
        "fingerprint": "f" * 64,
        "service_name": "eng-platform-web",
        "repository": "owner/eng-platform-web",
        "operation": operation,
        "head_sha": HEAD,
        "base_sha": BASE,
        "profile_hash": cloud_build.profile_for(service).fingerprint(),
        "executor_digest": DIGEST,
        "planner_hash": PLANNER_HASH if operation == "main_release" else "",
        "provider": "cloud_build",
        "status": "submission_pending",
    }
    value.update(overrides)
    return value


def _execution_for(
    service: CatalogService, *, operation: str = "pr_quality", **overrides
) -> dict:
    return _execution(
        operation=operation,
        service_name=service.service_name,
        repository=service.repository,
        profile_hash=cloud_build.profile_for(service).fingerprint(),
        **overrides,
    )


@pytest.fixture
def configured(monkeypatch):
    settings = cloud_build.config.release_orchestrator
    monkeypatch.setattr(settings, "enabled", True)
    monkeypatch.setattr(settings, "enabled_services", ("eng-platform-web",))
    monkeypatch.setattr(settings, "canary_services", ())
    monkeypatch.setattr(
        settings,
        "service_account",
        "release-quality@test-project.iam.gserviceaccount.com",
    )
    monkeypatch.setattr(settings, "quality_node_image", DIGEST)
    monkeypatch.setattr(settings, "quality_python_image", DIGEST)
    monkeypatch.setattr(settings, "release_planner_image", PLANNER)
    monkeypatch.setattr(settings, "postgres_image", POSTGRES)
    monkeypatch.setattr(cloud_build, "planner_hash", lambda: PLANNER_HASH)
    monkeypatch.setattr(cloud_build.config.cloud_build, "project_id", "test-project")
    monkeypatch.setattr(cloud_build.config.cloud_build, "region", "us-central1")
    monkeypatch.setattr(
        cloud_build.config.cloud_build,
        "evidence_bucket",
        "test-release-evidence",
    )
    monkeypatch.setattr(
        cloud_build.config.quality,
        "bucket",
        "test-quality-evidence",
    )
    monkeypatch.setattr(
        cloud_build.config.cloud_build,
        "repositories",
        {"eng-platform-web": REPOSITORY_RESOURCE},
    )
    monkeypatch.setattr(
        cloud_build.config.github, "platform_api_url", "https://platform.example"
    )


def test_location_uses_configured_regional_api_path(configured):
    assert cloud_build._location() == "projects/test-project/locations/us-central1"


def test_session_uses_application_default_credentials_with_cloud_scope(monkeypatch):
    credentials = object()
    default = mock.Mock(return_value=(credentials, "ignored-project"))
    authorized_session = mock.Mock(return_value=object())
    monkeypatch.setattr(cloud_build, "default", default)
    monkeypatch.setattr(cloud_build, "AuthorizedSession", authorized_session)

    result = cloud_build._session()

    assert result is authorized_session.return_value
    default.assert_called_once_with(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    authorized_session.assert_called_once_with(credentials)


def test_repository_requires_connected_repository_resource(configured, monkeypatch):
    monkeypatch.setattr(cloud_build.config.cloud_build, "repositories", {})
    with pytest.raises(
        cloud_build.ReleaseCloudBuildError, match="Connected repository"
    ):
        cloud_build._repository(_service())


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda settings, cb: setattr(settings, "enabled", False), "disabled"),
        (
            lambda settings, cb: setattr(settings, "enabled_services", ()),
            "not enabled",
        ),
        (
            lambda settings, cb: setattr(cb, "project_id", ""),
            "project and service account",
        ),
        (
            lambda settings, cb: setattr(settings, "service_account", ""),
            "project and service account",
        ),
    ],
)
def test_build_request_fails_closed_when_orchestrator_is_not_ready(
    configured, mutation, message
):
    mutation(cloud_build.config.release_orchestrator, cloud_build.config.cloud_build)
    with pytest.raises(cloud_build.ReleaseCloudBuildError, match=message):
        cloud_build.build_request(_execution(), _service())


def test_canary_service_is_allowed_without_auto_enable(configured, monkeypatch):
    settings = cloud_build.config.release_orchestrator
    monkeypatch.setattr(settings, "enabled_services", ())
    monkeypatch.setattr(settings, "canary_services", ("eng-platform-web",))

    request = cloud_build.build_request(_execution(), _service())

    assert request["steps"][0]["id"] == "prepare"


@pytest.mark.parametrize(
    ("field", "error_type"),
    [
        ("quality_node_image", ValueError),
        ("release_planner_image", cloud_build.ReleaseCloudBuildError),
    ],
)
def test_all_tooling_images_must_be_digest_pinned(
    configured, monkeypatch, field, error_type
):
    monkeypatch.setattr(cloud_build.config.release_orchestrator, field, "image:latest")

    with pytest.raises(error_type, match="pinned by digest"):
        cloud_build.build_request(_execution(), _service())


def test_pr_build_request_is_economical_fixed_and_source_pinned(configured):
    execution = _execution(
        request_command="curl https://attacker.invalid",
        credentials="request-secret",
    )
    service = _service()

    request = cloud_build.build_request(execution, service)

    assert request["source"] == {
        "connectedRepository": {
            "repository": REPOSITORY_RESOURCE,
            "revision": HEAD,
        }
    }
    assert request["options"] == {
        "machineType": "E2_STANDARD_2",
        "logging": "CLOUD_LOGGING_ONLY",
        "substitutionOption": "ALLOW_LOOSE",
    }
    assert request["timeout"] == "1800s"
    assert (
        "ENG_PLATFORM_EVIDENCE_BUCKET=test-quality-evidence"
        in request["steps"][0]["env"]
    )
    assert request["serviceAccount"].startswith("release-quality@")
    assert {
        "images",
        "artifacts",
        "availableSecrets",
        "secrets",
        "retry",
        "retries",
    }.isdisjoint(request)
    assert {"diskSizeGb", "pool", "privatePool", "retry", "retries"}.isdisjoint(
        request["options"]
    )
    assert [step["id"] for step in request["steps"]] == [
        "prepare",
        "quality",
        "publish-quality",
    ]
    prepare, quality, publish = request["steps"]
    assert prepare == {
        "id": "prepare",
        "name": DIGEST,
        "args": [
            "--mode",
            "prepare",
            "--service",
            "eng-platform-web",
            "--source",
            "/workspace",
            "--control-dir",
            "/eng-platform-control",
        ],
        "env": prepare["env"],
        "volumes": [{"name": "release-control", "path": "/eng-platform-control"}],
    }
    assert quality == {
        "id": "quality",
        "name": DIGEST,
        "args": [
            "--mode",
            "run",
            "--service",
            "eng-platform-web",
            "--source",
            "/workspace",
            "--scratch",
            "/tmp/eng-platform-quality",
            "--output-dir",
            "/eng-platform-output",
            "--external-dir",
            "/eng-platform-external",
        ],
        "env": quality["env"],
        "volumes": [{"name": "quality-output", "path": "/eng-platform-output"}],
        "waitFor": ["prepare"],
    }
    assert publish == {
        "id": "publish-quality",
        "name": DIGEST,
        "args": [
            "--mode",
            "publish",
            "--service",
            "eng-platform-web",
            "--manifest",
            "/eng-platform-output/quality-result.json",
            "--control-dir",
            "/eng-platform-control",
        ],
        "env": publish["env"],
        "volumes": [
            {"name": "quality-output", "path": "/eng-platform-output"},
            {"name": "release-control", "path": "/eng-platform-control"},
        ],
        "waitFor": ["quality"],
    }
    assert prepare["env"] == quality["env"] == publish["env"]
    assert "ENG_PLATFORM_RELEASE_HEAD_SHA=" + HEAD in quality["env"]
    assert "ENG_PLATFORM_RELEASE_BASE_SHA=" + BASE in quality["env"]
    assert "ENG_PLATFORM_RELEASE_OPERATION=pr_quality" in quality["env"]
    assert "ENG_PLATFORM_PROVIDER_RUN_ID=$BUILD_ID" in quality["env"]
    assert all(volume["name"] != "release-control" for volume in quality["volumes"])
    mounted_volume_names = [
        volume["name"]
        for step in request["steps"]
        for volume in step.get("volumes", [])
    ]
    assert set(mounted_volume_names) == {"release-control", "quality-output"}
    assert all(mounted_volume_names.count(name) >= 2 for name in mounted_volume_names)
    assert all(
        {"secretEnv", "retry", "retries"}.isdisjoint(step) for step in request["steps"]
    )
    assert request["substitutions"]["_HEAD_SHA"] == HEAD
    assert request["substitutions"]["_BASE_SHA"] == BASE
    assert request["substitutions"]["_EXECUTOR_DIGEST"] == DIGEST
    assert request["substitutions"]["_PROFILE_SHA256"] == (
        cloud_build.profile_for(service).fingerprint()
    )
    assert request["substitutions"]["_PLANNER_SHA256"] == ""
    assert request["substitutions"]["_PLANNER_DIGEST"] == ""
    assert request["tags"][-1] == "release-" + execution["execution_id"][:48]
    serialized = json.dumps(request, sort_keys=True)
    assert "curl https://attacker.invalid" not in serialized
    assert "request-secret" not in serialized


def test_main_build_splits_planner_from_trusted_plan_publisher(configured):
    execution = _execution(operation="main_release", planner_hash=PLANNER_HASH)
    request = cloud_build.build_request(execution, _service())

    assert [step["id"] for step in request["steps"]] == [
        "prepare",
        "quality",
        "publish-quality",
        "prepare-planner-volume",
        "release-plan",
        "publish-release-plan",
    ]
    permissions = request["steps"][3]
    assert permissions["name"] == DIGEST
    assert permissions["waitFor"] == ["publish-quality"]
    assert permissions["volumes"] == [
        {"name": "planner-output", "path": "/eng-platform-plan"},
        {"name": "release-control", "path": "/eng-platform-control"},
    ]
    assert "chown -R 1000:1000" in permissions["args"][1]

    planner = request["steps"][4]
    assert planner["name"] == PLANNER
    assert planner["args"] == [
        "--mode",
        "plan",
        "--source",
        "/workspace",
        "--output",
        "/eng-platform-plan/release-plan.json",
    ]
    assert planner["waitFor"] == ["prepare-planner-volume"]
    assert planner["volumes"] == [
        {"name": "planner-output", "path": "/eng-platform-plan"}
    ]
    assert all(volume["name"] != "release-control" for volume in planner["volumes"])
    assert f"ENG_PLATFORM_RELEASE_PLANNER_HASH={PLANNER_HASH}" in planner["env"]
    assert f"ENG_PLATFORM_RELEASE_PLANNER_IMAGE={PLANNER}" in planner["env"]
    assert "GIT_CONFIG_COUNT=1" in planner["env"]
    assert "GIT_CONFIG_KEY_0=safe.directory" in planner["env"]
    assert "GIT_CONFIG_VALUE_0=/workspace" in planner["env"]

    publisher = request["steps"][5]
    assert publisher["name"] == PLANNER
    assert publisher["args"] == [
        "--mode",
        "publish",
        "--manifest",
        "/eng-platform-plan/release-plan.json",
        "--control-dir",
        "/eng-platform-control",
    ]
    assert publisher["waitFor"] == ["release-plan"]
    assert not any(item.startswith("GIT_CONFIG_") for item in publisher["env"])
    assert publisher["volumes"] == [
        {"name": "planner-output", "path": "/eng-platform-plan"},
        {"name": "release-control", "path": "/eng-platform-control"},
    ]
    assert request["substitutions"]["_PLANNER_SHA256"] == PLANNER_HASH
    assert request["substitutions"]["_PLANNER_DIGEST"] == PLANNER


def test_api_profile_runs_container_smoke_without_control_volume(
    configured, monkeypatch
):
    service = _service(
        name="eng-platform-api",
        repository="owner/eng-platform-api",
        runtime="python",
        coverage=70,
    )
    monkeypatch.setattr(
        cloud_build.config.release_orchestrator,
        "enabled_services",
        (service.service_name,),
    )
    monkeypatch.setattr(
        cloud_build.config.cloud_build,
        "repositories",
        {service.service_name: REPOSITORY_RESOURCE},
    )

    request = cloud_build.build_request(
        _execution_for(service),
        service,
    )

    assert [step["id"] for step in request["steps"]] == [
        "prepare",
        "container-smoke",
        "quality",
        "publish-quality",
    ]
    smoke = request["steps"][1]
    assert smoke == {
        "id": "container-smoke",
        "name": DIGEST,
        "entrypoint": "/opt/eng-platform/api_container_smoke.sh",
        "args": ["/workspace", "/eng-platform-external"],
        "env": ["BUILD_ID=$BUILD_ID"],
        "volumes": [{"name": "quality-external", "path": "/eng-platform-external"}],
        "waitFor": ["prepare"],
    }
    quality = request["steps"][2]
    assert quality["waitFor"] == ["prepare", "container-smoke"]
    assert all(volume["name"] != "release-control" for volume in smoke["volumes"])
    assert all(volume["name"] != "release-control" for volume in quality["volumes"])


def test_postgres_profile_uses_pinned_image_and_isolated_test_dsn(
    configured, monkeypatch
):
    service = _service(
        name="cgm-sanplat-api",
        repository="owner/cgm-sanplat-api",
        runtime="python",
        coverage=70,
    )
    monkeypatch.setattr(
        cloud_build.config.release_orchestrator,
        "enabled_services",
        (service.service_name,),
    )
    monkeypatch.setattr(
        cloud_build.config.cloud_build,
        "repositories",
        {service.service_name: REPOSITORY_RESOURCE},
    )

    request = cloud_build.build_request(
        _execution_for(service),
        service,
    )

    assert [step["id"] for step in request["steps"]] == [
        "prepare",
        "postgres",
        "quality",
        "publish-quality",
    ]
    postgres = request["steps"][1]
    assert postgres["name"] == DIGEST
    assert postgres["entrypoint"] == "/bin/bash"
    assert postgres["args"][:3] == ["-euo", "pipefail", "-c"]
    assert '"$ENG_PLATFORM_POSTGRES_IMAGE"' in postgres["args"][3]
    assert "POSTGRES_DB=wm_test" in postgres["args"][3]
    assert "pg_isready -U postgres -d wm_test" in postgres["args"][3]
    assert (
        "docker exec eng-platform-postgres createdb -U postgres fnd_test"
        in postgres["args"][3]
    )
    assert postgres["env"] == [f"ENG_PLATFORM_POSTGRES_IMAGE={POSTGRES}"]
    assert postgres["waitFor"] == ["prepare"]
    assert "volumes" not in postgres

    quality = request["steps"][2]
    assert quality["waitFor"] == ["prepare", "postgres"]
    fnd_dsn = "postgresql://postgres:quality-only@127.0.0.1:5432/fnd_test"
    wm_dsn = "postgresql://postgres:quality-only@127.0.0.1:5432/wm_test"
    assert f"FND_TEST_POSTGRES_DSN={fnd_dsn}" in quality["env"]
    assert f"WM_TEST_POSTGRES_DSN={wm_dsn}" in quality["env"]
    assert quality["entrypoint"] == "/bin/bash"
    assert quality["args"][:3] == ["-euo", "pipefail", "-c"]
    assert "socat TCP-LISTEN:5432,bind=127.0.0.1" in quality["args"][3]
    assert "TCP:eng-platform-postgres:5432" in quality["args"][3]
    assert quality["args"][5:7] == ["--mode", "run"]
    assert all(volume["name"] != "release-control" for volume in quality["volumes"])


def test_postgres_profile_rejects_unpinned_database_image(configured, monkeypatch):
    service = _service(
        name="cgm-sanplat-api",
        repository="owner/cgm-sanplat-api",
        runtime="python",
        coverage=70,
    )
    monkeypatch.setattr(
        cloud_build.config.release_orchestrator,
        "enabled_services",
        (service.service_name,),
    )
    monkeypatch.setattr(
        cloud_build.config.release_orchestrator,
        "postgres_image",
        "postgres:16",
    )

    with pytest.raises(
        cloud_build.ReleaseCloudBuildError,
        match="PostgreSQL image must be pinned by digest",
    ):
        cloud_build.build_request(
            _execution_for(service),
            service,
        )


def test_sanplat_profile_keeps_bounded_long_timeout(configured, monkeypatch):
    service = _service(
        name="cgm-sanplat-api",
        repository="owner/cgm-sanplat-api",
        runtime="python",
        coverage=70,
    )
    settings = cloud_build.config.release_orchestrator
    monkeypatch.setattr(settings, "enabled_services", (service.service_name,))
    monkeypatch.setattr(
        cloud_build.config.cloud_build,
        "repositories",
        {service.service_name: REPOSITORY_RESOURCE},
    )
    execution = _execution_for(service)

    request = cloud_build.build_request(execution, service)

    assert request["timeout"] == "3600s"
    assert request["options"]["machineType"] == "E2_STANDARD_2"
    assert request["steps"][0]["name"] == DIGEST


def test_get_build_returns_payload_and_uses_short_timeout(configured, monkeypatch):
    session = mock.Mock()
    session.get.return_value = Response(payload={"id": "build-1", "status": "SUCCESS"})
    monkeypatch.setattr(cloud_build, "_session", lambda: session)

    result = cloud_build.get_build("build-1")

    assert result["status"] == "SUCCESS"
    session.get.assert_called_once_with(
        "https://cloudbuild.googleapis.com/v1/projects/test-project/locations/"
        "us-central1/builds/build-1",
        timeout=15,
    )


def test_get_build_hides_remote_error_details(configured, monkeypatch):
    session = mock.Mock()
    session.get.return_value = Response(status_code=403, payload={"token": "secret"})
    monkeypatch.setattr(cloud_build, "_session", lambda: session)

    with pytest.raises(
        cloud_build.ReleaseCloudBuildError, match="Unable to read release Cloud Build"
    ) as error:
        cloud_build.get_build("build-1")
    assert "secret" not in str(error.value)


def test_matching_build_requires_all_reconciliation_identity(configured, monkeypatch):
    execution = _execution()
    expected = cloud_build._expected_substitutions(execution)
    wrong = {
        "id": "wrong",
        "serviceAccount": "release-quality@test-project.iam.gserviceaccount.com",
        "source": {
            "connectedRepository": {
                "repository": REPOSITORY_RESOURCE,
                "revision": HEAD,
            }
        },
        "substitutions": {**expected, "_REQUEST_FINGERPRINT": "0" * 64},
    }
    matching = {
        "id": "matching",
        "serviceAccount": "release-quality@test-project.iam.gserviceaccount.com",
        "source": {
            "connectedRepository": {
                "repository": REPOSITORY_RESOURCE,
                "revision": HEAD,
            }
        },
        "substitutions": expected,
    }
    session = mock.Mock()
    session.get.return_value = Response(payload={"builds": [wrong, matching]})
    monkeypatch.setattr(cloud_build, "_session", lambda: session)

    assert cloud_build._matching_build(execution) == matching
    _, kwargs = session.get.call_args
    assert kwargs["params"] == {
        "filter": f'tags="release-{execution["execution_id"][:48]}"',
        "pageSize": "20",
    }


@pytest.mark.parametrize(
    "source",
    [
        {},
        {
            "connectedRepository": {
                "repository": "projects/other/locations/us-central1/connections/x/repositories/y",
                "revision": HEAD,
            }
        },
        {
            "connectedRepository": {
                "repository": REPOSITORY_RESOURCE,
                "revision": "0" * 40,
            }
        },
    ],
)
def test_matching_build_rejects_connected_repository_source_mismatch(
    configured, monkeypatch, source
):
    execution = _execution()
    build = {
        "id": "untrusted-build",
        "serviceAccount": "release-quality@test-project.iam.gserviceaccount.com",
        "source": source,
        "substitutions": cloud_build._expected_substitutions(execution),
    }
    session = mock.Mock()
    session.get.return_value = Response(payload={"builds": [build]})
    monkeypatch.setattr(cloud_build, "_session", lambda: session)

    assert cloud_build._matching_build(execution) is None


def test_matching_build_returns_none_and_fails_closed_on_api_error(
    configured, monkeypatch
):
    session = mock.Mock()
    session.get.return_value = Response(payload={"builds": []})
    monkeypatch.setattr(cloud_build, "_session", lambda: session)
    assert cloud_build._matching_build(_execution()) is None

    session.get.return_value = Response(status_code=500)
    with pytest.raises(
        cloud_build.ReleaseCloudBuildError, match="reconciliation failed"
    ):
        cloud_build._matching_build(_execution())


def test_bind_requires_build_id_and_persists_only_public_metadata(monkeypatch):
    bind = mock.Mock(return_value={"execution_id": "execution", "build_id": "build-1"})
    monkeypatch.setattr(cloud_build.release_executions, "bind_build", bind)

    result = cloud_build._bind(
        "execution",
        {
            "id": "build-1",
            "status": "WORKING",
            "logUrl": "https://console/build-1",
            "name": "projects/p/locations/r/builds/build-1",
        },
    )

    assert result["build_id"] == "build-1"
    bind.assert_called_once_with(
        "execution",
        build_id="build-1",
        provider_status="WORKING",
        logs_url="https://console/build-1",
        build_name="projects/p/locations/r/builds/build-1",
        submission_reconciled_at=mock.ANY,
    )
    with pytest.raises(cloud_build.ReleaseCloudBuildError, match="build ID"):
        cloud_build._bind("execution", {})


def test_submit_rejects_unknown_or_non_cloud_execution(configured, monkeypatch):
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: None)
    with pytest.raises(cloud_build.ReleaseCloudBuildError, match="Unknown"):
        cloud_build.submit("missing", _service())

    monkeypatch.setattr(
        cloud_build.release_executions,
        "get",
        lambda _: _execution(provider="github_actions"),
    )
    with pytest.raises(cloud_build.ReleaseCloudBuildError, match="not Cloud Build"):
        cloud_build.submit("execution", _service())


def test_submit_returns_already_bound_execution_without_api_calls(
    configured, monkeypatch
):
    execution = _execution(build_id="build-1")
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    matching = mock.Mock()
    monkeypatch.setattr(cloud_build, "_matching_build", matching)

    assert cloud_build.submit(execution["execution_id"], _service()) is execution
    matching.assert_not_called()


def test_submit_recovers_existing_matching_build_before_claim(configured, monkeypatch):
    execution = _execution()
    recovered = {"id": "build-recovered"}
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(cloud_build, "_matching_build", lambda _: recovered)
    bind = mock.Mock(return_value={"build_id": "build-recovered"})
    monkeypatch.setattr(cloud_build, "_bind", bind)
    claim = mock.Mock()
    monkeypatch.setattr(cloud_build.release_executions, "claim_submission", claim)

    assert cloud_build.submit(execution["execution_id"], _service()) == {
        "build_id": "build-recovered"
    }
    bind.assert_called_once_with(execution["execution_id"], recovered)
    claim.assert_not_called()


def test_submit_refuses_when_another_process_claimed_submission(
    configured, monkeypatch
):
    execution = _execution()
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(cloud_build, "_matching_build", lambda _: None)
    monkeypatch.setattr(
        cloud_build.release_executions, "claim_submission", lambda _: False
    )

    with pytest.raises(cloud_build.ReleaseCloudBuildError, match="being reconciled"):
        cloud_build.submit(execution["execution_id"], _service())


def test_submit_marks_uncertain_when_transport_raises(configured, monkeypatch):
    execution = _execution()
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(cloud_build, "_matching_build", lambda _: None)
    monkeypatch.setattr(
        cloud_build.release_executions, "claim_submission", lambda _: True
    )
    monkeypatch.setattr(cloud_build, "build_request", lambda *_: {"build": "request"})
    session = mock.Mock()
    session.post.side_effect = TimeoutError("network timeout with sensitive details")
    monkeypatch.setattr(cloud_build, "_session", lambda: session)
    save = mock.Mock()
    monkeypatch.setattr(cloud_build.release_executions, "save", save)

    with pytest.raises(cloud_build.ReleaseCloudBuildError, match="is uncertain") as err:
        cloud_build.submit(execution["execution_id"], _service())

    assert "sensitive" not in str(err.value)
    save.assert_called_once_with(
        execution["execution_id"], status="unknown", uncertain_since=mock.ANY
    )


def test_submit_marks_terminal_failure_for_explicit_http_error(configured, monkeypatch):
    execution = _execution()
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(cloud_build, "_matching_build", lambda _: None)
    monkeypatch.setattr(
        cloud_build.release_executions, "claim_submission", lambda _: True
    )
    monkeypatch.setattr(cloud_build, "build_request", lambda *_: {})
    session = mock.Mock()
    session.post.return_value = Response(status_code=429, payload={"error": "secret"})
    monkeypatch.setattr(cloud_build, "_session", lambda: session)
    save = mock.Mock()
    monkeypatch.setattr(cloud_build.release_executions, "save", save)

    with pytest.raises(cloud_build.ReleaseCloudBuildError, match="failed: 429") as err:
        cloud_build.submit(execution["execution_id"], _service())

    assert "secret" not in str(err.value)
    save.assert_called_once_with(
        execution["execution_id"], status="failed", submission_status_code=429
    )


def test_submit_records_google_validation_error_privately(configured, monkeypatch):
    execution = _execution()
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(cloud_build, "_matching_build", lambda _: None)
    monkeypatch.setattr(
        cloud_build.release_executions, "claim_submission", lambda _: True
    )
    monkeypatch.setattr(cloud_build, "build_request", lambda *_: {})
    session = mock.Mock()
    session.post.return_value = Response(
        status_code=400,
        payload={"error": {"message": "Invalid build field: source revision"}},
    )
    monkeypatch.setattr(cloud_build, "_session", lambda: session)
    save = mock.Mock()
    monkeypatch.setattr(cloud_build.release_executions, "save", save)

    with pytest.raises(cloud_build.ReleaseCloudBuildError, match="failed: 400") as err:
        cloud_build.submit(execution["execution_id"], _service())

    assert "source revision" not in str(err.value)
    save.assert_called_once_with(
        execution["execution_id"],
        status="failed",
        submission_status_code=400,
        submission_error="Invalid build field: source revision",
    )


@pytest.mark.parametrize(
    "build",
    [
        {"substitutions": {"_REQUEST_FINGERPRINT": "f" * 64}},
        {"id": "build-1", "substitutions": {"_REQUEST_FINGERPRINT": "0" * 64}},
    ],
)
def test_submit_rejects_uncertain_response_identity(configured, monkeypatch, build):
    execution = _execution()
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(cloud_build, "_matching_build", lambda _: None)
    monkeypatch.setattr(
        cloud_build.release_executions, "claim_submission", lambda _: True
    )
    monkeypatch.setattr(cloud_build, "build_request", lambda *_: {})
    session = mock.Mock()
    session.post.return_value = Response(payload={"metadata": {"build": build}})
    monkeypatch.setattr(cloud_build, "_session", lambda: session)
    save = mock.Mock()
    monkeypatch.setattr(cloud_build.release_executions, "save", save)

    with pytest.raises(
        cloud_build.ReleaseCloudBuildError, match="identity is uncertain"
    ):
        cloud_build.submit(execution["execution_id"], _service())
    save.assert_called_once_with(
        execution["execution_id"], status="unknown", uncertain_since=mock.ANY
    )


def test_reconcile_uncertain_rejects_unknown_execution(configured, monkeypatch):
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: None)
    with pytest.raises(cloud_build.ReleaseCloudBuildError, match="Unknown"):
        cloud_build.reconcile_uncertain_submission("missing", _service())


@pytest.mark.parametrize(
    "execution",
    [
        _execution(provider="github_actions", status="unknown"),
        _execution(build_id="build-1", status="unknown"),
        _execution(status="submission_pending"),
    ],
)
def test_reconcile_uncertain_is_noop_when_not_eligible(
    configured, monkeypatch, execution
):
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    matching = mock.Mock()
    monkeypatch.setattr(cloud_build, "_matching_build", matching)

    assert (
        cloud_build.reconcile_uncertain_submission(
            execution["execution_id"], _service()
        )
        is execution
    )
    matching.assert_not_called()


def test_reconcile_uncertain_binds_matching_build_without_retry(
    configured, monkeypatch
):
    execution = _execution(status="unknown")
    recovered = {"id": "build-recovered", "status": "WORKING"}
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(cloud_build, "_matching_build", lambda _: recovered)
    bind = mock.Mock(return_value={**execution, "build_id": "build-recovered"})
    retry = mock.Mock()
    monkeypatch.setattr(cloud_build, "_bind", bind)
    monkeypatch.setattr(
        cloud_build.release_executions, "reconcile_submission_absent", retry
    )

    result = cloud_build.reconcile_uncertain_submission(
        execution["execution_id"], _service()
    )

    assert result["build_id"] == "build-recovered"
    bind.assert_called_once_with(execution["execution_id"], recovered)
    retry.assert_not_called()


class _Clock:
    current = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, zone):
        assert zone is timezone.utc
        return cls.current

    @classmethod
    def fromisoformat(cls, value):
        return datetime.fromisoformat(value)


@pytest.mark.parametrize(
    "uncertain_field",
    [
        {"uncertain_since": "2026-09-22T11:58:01+00:00"},
        {"updated_at": "2026-09-22T11:58:01Z"},
        {"uncertain_since": "not-a-timestamp"},
    ],
)
def test_reconcile_uncertain_respects_grace_and_invalid_timestamp(
    configured, monkeypatch, uncertain_field
):
    execution = _execution(status="unknown", **uncertain_field)
    monkeypatch.setattr(cloud_build, "datetime", _Clock)
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(cloud_build, "_matching_build", lambda _: None)
    retry = mock.Mock()
    submit = mock.Mock()
    monkeypatch.setattr(
        cloud_build.release_executions, "reconcile_submission_absent", retry
    )
    monkeypatch.setattr(cloud_build, "submit", submit)

    assert (
        cloud_build.reconcile_uncertain_submission(
            execution["execution_id"], _service()
        )
        is execution
    )
    retry.assert_not_called()
    submit.assert_not_called()


def test_reconcile_uncertain_permits_exactly_one_retry_after_grace(
    configured, monkeypatch
):
    execution = _execution(
        status="unknown",
        uncertain_since="2026-09-22T11:57:59Z",
        reconciliation_attempts=0,
    )
    monkeypatch.setattr(cloud_build, "datetime", _Clock)
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(cloud_build, "_matching_build", lambda _: None)
    retry = mock.Mock()
    submitted = {**execution, "build_id": "retry-build"}
    submit = mock.Mock(return_value=submitted)
    monkeypatch.setattr(
        cloud_build.release_executions, "reconcile_submission_absent", retry
    )
    monkeypatch.setattr(cloud_build, "submit", submit)

    result = cloud_build.reconcile_uncertain_submission(
        execution["execution_id"], _service()
    )

    assert result is submitted
    retry.assert_called_once_with(execution["execution_id"])
    submit.assert_called_once_with(execution["execution_id"], _service())


def test_reconcile_second_uncertain_result_never_retries_again(configured, monkeypatch):
    execution = _execution(
        status="unknown",
        uncertain_since="2026-09-22T11:00:00Z",
        reconciliation_attempts=1,
    )
    monkeypatch.setattr(cloud_build, "datetime", _Clock)
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(cloud_build, "_matching_build", lambda _: None)
    retry = mock.Mock()
    submit = mock.Mock()
    monkeypatch.setattr(
        cloud_build.release_executions, "reconcile_submission_absent", retry
    )
    monkeypatch.setattr(cloud_build, "submit", submit)

    assert (
        cloud_build.reconcile_uncertain_submission(
            execution["execution_id"], _service()
        )
        is execution
    )
    retry.assert_not_called()
    submit.assert_not_called()


@pytest.mark.parametrize("nested", [True, False])
def test_submit_binds_verified_operation_response(configured, monkeypatch, nested):
    execution = _execution()
    build = {
        "id": "build-1",
        "substitutions": {"_REQUEST_FINGERPRINT": execution["fingerprint"]},
    }
    payload = {"metadata": {"build": build}} if nested else build
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    monkeypatch.setattr(cloud_build, "_matching_build", lambda _: None)
    monkeypatch.setattr(
        cloud_build.release_executions, "claim_submission", lambda _: True
    )
    request = {"fixed": "request"}
    monkeypatch.setattr(cloud_build, "build_request", lambda *_: request)
    session = mock.Mock()
    session.post.return_value = Response(payload=payload)
    monkeypatch.setattr(cloud_build, "_session", lambda: session)
    bind = mock.Mock(return_value={"build_id": "build-1"})
    monkeypatch.setattr(cloud_build, "_bind", bind)

    assert cloud_build.submit(execution["execution_id"], _service()) == {
        "build_id": "build-1"
    }
    session.post.assert_called_once_with(
        "https://cloudbuild.googleapis.com/v1/projects/test-project/locations/"
        "us-central1/builds",
        json=request,
        timeout=30,
    )
    bind.assert_called_once_with(execution["execution_id"], build)


@pytest.mark.parametrize(
    "execution",
    [None, _execution(), _execution(build_id="build-1", operation="main_release")],
)
def test_cancel_is_noop_without_active_pr_build(configured, monkeypatch, execution):
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    matching = mock.Mock(return_value=None)
    monkeypatch.setattr(cloud_build, "_matching_build", matching)
    session = mock.Mock()
    monkeypatch.setattr(cloud_build, "_session", lambda: session)

    assert cloud_build.cancel("execution") is False
    session.post.assert_not_called()
    if execution and execution.get("operation") == "pr_quality":
        matching.assert_called_once_with(execution)
    else:
        matching.assert_not_called()


def test_cancel_recovers_and_binds_exact_unbound_pr_build_before_cancelling(
    configured, monkeypatch
):
    execution = _execution()
    recovered = {"id": "recovered-build"}
    bound = {**execution, "build_id": "recovered-build"}
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    matching = mock.Mock(return_value=recovered)
    bind = mock.Mock(return_value=bound)
    monkeypatch.setattr(cloud_build, "_matching_build", matching)
    monkeypatch.setattr(cloud_build, "_bind", bind)
    session = mock.Mock()
    session.post.return_value = Response(status_code=200)
    monkeypatch.setattr(cloud_build, "_session", lambda: session)
    save = mock.Mock()
    monkeypatch.setattr(cloud_build.release_executions, "save", save)

    assert cloud_build.cancel(execution["execution_id"]) is True

    matching.assert_called_once_with(execution)
    bind.assert_called_once_with(execution["execution_id"], recovered)
    session.post.assert_called_once_with(
        "https://cloudbuild.googleapis.com/v1/projects/test-project/locations/"
        "us-central1/builds/recovered-build:cancel",
        timeout=15,
    )
    save.assert_called_once_with(
        execution["execution_id"], status="failed", superseded=True
    )


@pytest.mark.parametrize("status_code", [200, 409])
def test_cancel_marks_superseded_pr_failed(configured, monkeypatch, status_code):
    execution = _execution(build_id="build-1")
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    session = mock.Mock()
    session.post.return_value = Response(status_code=status_code)
    monkeypatch.setattr(cloud_build, "_session", lambda: session)
    save = mock.Mock()
    monkeypatch.setattr(cloud_build.release_executions, "save", save)

    assert cloud_build.cancel(execution["execution_id"]) is True
    session.post.assert_called_once_with(
        "https://cloudbuild.googleapis.com/v1/projects/test-project/locations/"
        "us-central1/builds/build-1:cancel",
        timeout=15,
    )
    save.assert_called_once_with(
        execution["execution_id"], status="failed", superseded=True
    )


def test_cancel_fails_closed_on_remote_error(configured, monkeypatch):
    execution = _execution(build_id="build-1")
    monkeypatch.setattr(cloud_build.release_executions, "get", lambda _: execution)
    session = mock.Mock()
    session.post.return_value = Response(status_code=500)
    monkeypatch.setattr(cloud_build, "_session", lambda: session)
    save = mock.Mock()
    monkeypatch.setattr(cloud_build.release_executions, "save", save)

    with pytest.raises(cloud_build.ReleaseCloudBuildError, match="Unable to cancel"):
        cloud_build.cancel(execution["execution_id"])
    save.assert_not_called()
