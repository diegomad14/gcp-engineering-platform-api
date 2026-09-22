"""Economic, immutable Cloud Build request and idempotency contracts."""

import json
from unittest import mock
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest

from eng_platform_api.config import config
from eng_platform_api.models import DeploymentItem
from eng_platform_api.services import catalog, cloud_build, deployment_executions
from eng_platform_api.services.release_profiles import profile_for


class _Snapshot:
    def __init__(self, value):
        self.value = value
        self.exists = value is not None

    def to_dict(self):
        return dict(self.value) if self.value is not None else None


class _Transaction:
    def set(self, document, value):
        document.value = dict(value)

    def update(self, document, changes):
        document.value.update(changes)


class _Client:
    def transaction(self):
        return _Transaction()


class _Document:
    def __init__(self):
        self.value = None
        self._client = _Client()

    def get(self, transaction=None):
        return _Snapshot(self.value)

    def set(self, value, merge=False):
        if merge and self.value:
            self.value.update(value)
        else:
            self.value = dict(value)


class _Collection:
    def __init__(self):
        self.documents = {}

    def document(self, name):
        return self.documents.setdefault(name, _Document())


@pytest.fixture(autouse=True)
def cloud_build_settings(monkeypatch):
    monkeypatch.setattr(config, "mock_mode", True)
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


def test_submit_claim_allows_only_one_external_post(monkeypatch):
    service = catalog.get_service("eng-platform-api")
    assert service is not None
    item = _item()
    fingerprint = cloud_build.fingerprint(item, service)
    deployment_executions.reserve(
        item.id,
        provider="cloud_build",
        fingerprint=fingerprint,
        service_name=item.service_name,
        repository=item.repository,
        sha=item.sha,
        tag=item.tag,
        kind=item.kind,
    )
    assert deployment_executions.claim_submission(item.id) is True

    session = mock.MagicMock()
    monkeypatch.setattr(cloud_build, "_matching_build", lambda *_: None)
    monkeypatch.setattr(cloud_build, "_session", lambda: session)

    with pytest.raises(cloud_build.CloudBuildError, match="being reconciled"):
        cloud_build.submit(item, service, reason="concurrent-retry")

    session.post.assert_not_called()


def test_provider_transition_is_single_and_preserves_identity():
    service = catalog.get_service("eng-platform-api")
    assert service is not None
    item = _item()
    fingerprint = cloud_build.fingerprint(item, service)
    deployment_executions.reserve(
        item.id,
        provider="github_actions",
        fingerprint=fingerprint,
        service_name=item.service_name,
        repository=item.repository,
        sha=item.sha,
        tag=item.tag,
        kind=item.kind,
        authorization_jti="auth-1",
    )

    first = deployment_executions.transition_provider(
        item.id,
        from_provider="github_actions",
        to_provider="cloud_build",
        reason="quota",
    )
    second = deployment_executions.transition_provider(
        item.id,
        from_provider="github_actions",
        to_provider="cloud_build",
        reason="duplicate",
    )

    assert first["provider"] == second["provider"] == "cloud_build"
    assert second["fingerprint"] == fingerprint
    assert second["reason"] == "quota"
    assert second["status"] == "SUBMISSION_PENDING"


def test_submit_binds_exact_accepted_build(monkeypatch):
    service = catalog.get_service("eng-platform-api")
    assert service is not None
    item = _item()
    fingerprint = cloud_build.fingerprint(item, service)
    response = mock.MagicMock(status_code=200)
    response.json.return_value = {
        "metadata": {
            "build": {
                "id": "build-accepted",
                "status": "QUEUED",
                "logUrl": "https://logs.invalid/accepted",
                "substitutions": {"_REQUEST_FINGERPRINT": fingerprint},
            }
        }
    }
    session = mock.MagicMock()
    session.post.return_value = response
    monkeypatch.setattr(cloud_build, "_matching_build", lambda *_: None)
    monkeypatch.setattr(cloud_build, "_session", lambda: session)

    result = cloud_build.submit(item, service, reason="quota")

    assert result.status == "QUEUED"
    assert result.logs_url == "https://logs.invalid/accepted"
    assert deployment_executions.get(item.id)["build_id"] == "build-accepted"
    session.post.assert_called_once()


@pytest.mark.parametrize("failure", ["exception", "http", "identity"])
def test_submit_fails_closed_without_retrying_uncertain_identity(monkeypatch, failure):
    service = catalog.get_service("eng-platform-api")
    assert service is not None
    item = _item()
    session = mock.MagicMock()
    if failure == "exception":
        session.post.side_effect = TimeoutError("uncertain")
    else:
        response = mock.MagicMock(status_code=403 if failure == "http" else 200)
        response.json.return_value = {"metadata": {"build": {"id": "wrong"}}}
        session.post.return_value = response
    monkeypatch.setattr(cloud_build, "_matching_build", lambda *_: None)
    monkeypatch.setattr(cloud_build, "_session", lambda: session)

    with pytest.raises(cloud_build.CloudBuildError):
        cloud_build.submit(item, service, reason="quota")

    state = deployment_executions.get(item.id)
    assert state is not None
    assert state["status"] == (
        "SUBMISSION_FAILED" if failure == "http" else "SUBMISSION_UNKNOWN"
    )


def _bound_item() -> DeploymentItem:
    item = _item()
    deployment_executions.reserve(
        item.id,
        provider="cloud_build",
        fingerprint="c" * 64,
        service_name=item.service_name,
        repository=item.repository,
        sha=item.sha,
        tag=item.tag,
        kind=item.kind,
    )
    deployment_executions.save(item.id, build_id="build-1")
    return item


@pytest.mark.parametrize(
    ("build_status", "expected_status", "expected_stage"),
    [
        ("QUEUED", "QUEUED", "queued"),
        ("WORKING", "BUILDING", "build"),
    ],
)
def test_refresh_projects_nonterminal_build_state(
    monkeypatch, build_status, expected_status, expected_stage
):
    item = _bound_item()
    monkeypatch.setattr(
        cloud_build,
        "get_build",
        lambda _: {"status": build_status, "logUrl": "https://logs.invalid/build"},
    )

    refreshed = cloud_build.refresh(item)

    assert refreshed.status == expected_status
    assert refreshed.current_stage == expected_stage
    assert refreshed.logs_url == "https://logs.invalid/build"


@pytest.mark.parametrize("build_status", ["SUCCESS", "FAILURE", "TIMEOUT"])
def test_refresh_reconciles_terminal_build_once(monkeypatch, build_status):
    item = _bound_item()
    reconcile = mock.Mock()
    monkeypatch.setattr(cloud_build, "_reconcile", reconcile)
    monkeypatch.setattr(
        cloud_build,
        "get_build",
        lambda _: {"status": build_status, "logUrl": "https://logs.invalid/build"},
    )

    cloud_build.refresh(item)

    reconcile.assert_called_once()


def test_reconcile_distinguishes_live_healthy_unchanged_and_unknown(monkeypatch):
    service = catalog.get_service("eng-platform-api")
    assert service is not None
    item = _item()
    expected = f"{item.service_name}-ep-{cloud_build.fingerprint(item, service)[:10]}"
    runtime = {
        "trafficStatuses": [{"revision": expected, "percent": 100}],
        "uri": "https://service.invalid",
    }
    monkeypatch.setattr(cloud_build, "_runtime_service", lambda _: runtime)
    monkeypatch.setattr(cloud_build, "_healthy", lambda *_: True)

    cloud_build._reconcile(item, {"status": "SUCCESS"})
    assert item.status == "SUCCEEDED"
    assert item.production_revision == expected

    item = _item()
    runtime["trafficStatuses"] = [{"revision": "previous", "percent": 100}]
    cloud_build._reconcile(item, {"status": "FAILURE"})
    assert item.status == "FAILED"
    assert "traffic unchanged" in item.error

    item = _item()
    runtime["trafficStatuses"] = [{"revision": expected, "percent": 100}]
    monkeypatch.setattr(cloud_build, "_healthy", lambda *_: False)
    cloud_build._reconcile(item, {"status": "SUCCESS"})
    assert item.status == "UNKNOWN"


def test_reconcile_failure_is_indeterminate(monkeypatch):
    item = _item()
    monkeypatch.setattr(
        cloud_build, "_runtime_service", mock.Mock(side_effect=RuntimeError("down"))
    )

    cloud_build._reconcile(item, {"status": "SUCCESS"})

    assert item.status == "UNKNOWN"
    assert "indeterminate" in item.error


def test_get_build_and_health_fail_closed(monkeypatch):
    response = mock.MagicMock(status_code=503)
    session = mock.MagicMock()
    session.get.return_value = response
    monkeypatch.setattr(cloud_build, "_session", lambda: session)
    with pytest.raises(cloud_build.CloudBuildError, match="read failed"):
        cloud_build.get_build("build-1")

    monkeypatch.setattr(
        cloud_build.urllib.request,
        "urlopen",
        mock.Mock(side_effect=TimeoutError("down")),
    )
    assert cloud_build._healthy("https://service.invalid", "/health") is False


def test_firestore_execution_store_is_atomic(monkeypatch):
    collection = _Collection()
    monkeypatch.setattr(deployment_executions, "_collection", lambda: collection)
    monkeypatch.setattr(
        "google.cloud.firestore.transactional", lambda function: function
    )
    item = _item()
    fingerprint = "d" * 64

    created = deployment_executions.reserve(
        item.id,
        provider="github_actions",
        fingerprint=fingerprint,
        service_name=item.service_name,
        repository=item.repository,
        sha=item.sha,
        tag=item.tag,
        kind=item.kind,
        authorization_jti="ticket",
    )
    repeated = deployment_executions.reserve(
        item.id,
        provider="github_actions",
        fingerprint=fingerprint,
        service_name=item.service_name,
        repository=item.repository,
        sha=item.sha,
        tag=item.tag,
        kind=item.kind,
    )
    assert created["fingerprint"] == repeated["fingerprint"] == fingerprint

    transitioned = deployment_executions.transition_provider(
        item.id,
        from_provider="github_actions",
        to_provider="cloud_build",
        reason="quota",
    )
    assert transitioned["provider"] == "cloud_build"
    assert deployment_executions.claim_submission(item.id) is True
    assert deployment_executions.claim_submission(item.id) is False

    saved = deployment_executions.save(item.id, build_id="build-1")
    assert saved["build_id"] == "build-1"
    accepted, changed = deployment_executions.accept_event(
        item.id, 2, stage="build", stage_status="running"
    )
    assert changed is True
    assert accepted["event_sequence"] == 2
    repeated_event, changed = deployment_executions.accept_event(
        item.id, 1, stage="queued"
    )
    assert changed is False
    assert repeated_event["event_sequence"] == 2


def test_firestore_execution_store_rejects_identity_and_transition(monkeypatch):
    collection = _Collection()
    monkeypatch.setattr(deployment_executions, "_collection", lambda: collection)
    monkeypatch.setattr(
        "google.cloud.firestore.transactional", lambda function: function
    )
    item = _item()
    deployment_executions.reserve(
        item.id,
        provider="github_actions",
        fingerprint="a" * 64,
        service_name=item.service_name,
        repository=item.repository,
        sha=item.sha,
        tag=item.tag,
        kind=item.kind,
    )
    with pytest.raises(ValueError, match="identity"):
        deployment_executions.reserve(
            item.id,
            provider="github_actions",
            fingerprint="b" * 64,
            service_name=item.service_name,
            repository=item.repository,
            sha=item.sha,
            tag=item.tag,
            kind=item.kind,
        )
    with pytest.raises(ValueError, match="transition"):
        deployment_executions.transition_provider(
            item.id,
            from_provider="unexpected",
            to_provider="cloud_build",
            reason="quota",
        )
    with pytest.raises(KeyError):
        deployment_executions.claim_submission("missing")


def test_execution_collection_requires_explicit_production_configuration(monkeypatch):
    monkeypatch.setattr(config.cloud_build, "enabled", False)
    assert deployment_executions._collection() is None

    monkeypatch.setattr(config.cloud_build, "enabled", True)
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.cloud_build, "execution_collection", "executions")
    monkeypatch.setattr(config.cloud_build, "project_id", "project")
    firestore_client = mock.Mock()
    with mock.patch("google.cloud.firestore.Client", return_value=firestore_client):
        deployment_executions._collection()
    firestore_client.collection.assert_called_once_with("executions")


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


def test_executor_persists_release_summary_in_shared_workspace(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / "docker/release-executor/release_executor.py"
    spec = spec_from_file_location("release_executor_summary", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    for name, value in {
        "CGM_DEPLOYMENT_ID": "42",
        "CGM_REQUEST_FINGERPRINT": "fingerprint",
        "CGM_SERVICE": "eng-platform-api",
        "CGM_RELEASE_SHA": "a" * 40,
        "CGM_RELEASE_TAG": "v1.2.3",
    }.items():
        monkeypatch.setenv(name, value)

    module.write_summary({"production_revision": "revision-1"})

    summary = json.loads((tmp_path / ".eng-platform-release-result.json").read_text())
    assert summary["deployment_id"] == "42"
    assert summary["production_revision"] == "revision-1"


@pytest.mark.parametrize(
    "workflow_name", ["platform-deploy.yml", "platform-rollback.yml"]
)
def test_thin_workflow_authenticates_before_pulling_private_executor(workflow_name):
    workflow = (
        Path(__file__).parents[1] / ".github/workflows" / workflow_name
    ).read_text()

    assert workflow.index("gcloud auth configure-docker") < workflow.index(
        '"${{ vars.ENG_PLATFORM_RELEASE_EXECUTOR_IMAGE }}"'
    )
    assert (
        '--env CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE="/workspace/$credential_file"'
        in workflow
    )
    assert "--env CLOUDSDK_CONFIG=/tmp/gcloud" in workflow
