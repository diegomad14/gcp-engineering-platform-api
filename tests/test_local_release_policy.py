"""Regression coverage for default-off adoption and authenticated registration."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from eng_platform_api.main import app
from eng_platform_api.models import CatalogService, QualityReport
from eng_platform_api.routers import release_execution as api
from eng_platform_api.services import quality_store
from eng_platform_api.services import local_release_policy as policy


@pytest.fixture
def issuance(monkeypatch):
    service = CatalogService(
        service_name="test-api",
        repository="test-org/test-repo",
        owner="platform",
        project_id="test-project",
        region="us-central1",
        deployment_ready=True,
        quality={"enabled": True, "profile": "python", "policy_version": "oss-v2"},
    )
    now = datetime.now(timezone.utc).isoformat()
    report = QualityReport(
        service_name=service.service_name,
        repository=service.repository,
        commit_sha="a" * 40,
        profile="python",
        generated_at=now,
        received_at=now,
        quality_gate_status="PASSED",
        policy_version="oss-v2",
        base_sha="b" * 40,
        coverage=90,
        coverage_threshold=70,
        differential_coverage=90,
        differential_threshold=80,
        changed_lines=10,
        covered_changed_lines=9,
        checks=[
            {"name": name, "category": name, "status": "PASSED"}
            for name in (
                "setup",
                "tests",
                "lint",
                "format",
                "typecheck",
                "sast",
                "dependencies",
                "secrets",
                "misconfiguration",
                "differential_coverage",
            )
        ],
    )
    monkeypatch.setattr(api, "require_deployer", lambda _request: "test-operator")
    monkeypatch.setattr(api.catalog, "get_service", lambda _name: service)
    monkeypatch.setattr(quality_store, "get_report", lambda *_args: report)
    monkeypatch.setattr(
        api.release_authorization,
        "issue",
        lambda **_kwargs: ("synthetic-ticket-not-a-credential", {"exp": 9999999999}),
    )
    monkeypatch.setattr(api.config, "mock_mode", False)
    monkeypatch.setattr(
        api.config.release_execution, "remote_activation_enabled", False
    )
    payload = dict(
        release_id="test-release",
        repository=service.repository,
        service_name=service.service_name,
        source_sha="a" * 40,
        tag="v1.2.3",
        artifact_digest="sha256:" + "c" * 64,
        target="test-project/us-central1/test-api",
        configuration_hash="d" * 64,
    )
    return SimpleNamespace(client=TestClient(app), payload=payload, report=report)


@pytest.mark.parametrize(
    "operation", ["publish", "candidate", "register", "promote", "rollback"]
)
def test_disabled_activation_never_issues_effect_ticket(issuance, operation):
    payload = {**issuance.payload, "operation": operation}
    if operation == "publish":
        payload["target"] = "test-org/test-repo:oss-v2"
    response = issuance.client.post(
        "/api/internal/release-execution/authorizations/issue",
        json=payload,
    )
    assert response.status_code >= 400, "Disabled deployment issued an effect ticket"


def test_expired_quality_never_issues_ticket(issuance, monkeypatch):
    monkeypatch.setattr(api.config.release_execution, "remote_activation_enabled", True)
    monkeypatch.setattr(api.config.release_execution, "allowed_services", ("test-api",))
    monkeypatch.setattr(api.config, "mock_mode", True)
    issuance.report.generated_at = "2000-01-01T00:00:00+00:00"
    response = issuance.client.post(
        "/api/internal/release-execution/authorizations/issue",
        json={**issuance.payload, "operation": "candidate"},
    )
    assert response.status_code >= 400, "Expired quality authorized a candidate"


def test_valid_quality_and_enabled_target_issues_ticket(issuance, monkeypatch):
    monkeypatch.setattr(api.config.release_execution, "remote_activation_enabled", True)
    monkeypatch.setattr(api.config.release_execution, "allowed_services", ("test-api",))
    monkeypatch.setattr(api.config, "mock_mode", True)
    response = issuance.client.post(
        "/api/internal/release-execution/authorizations/issue",
        json={**issuance.payload, "operation": "candidate"},
    )
    assert response.status_code == 200, response.text


def test_adoption_blocks_actions_even_when_local_is_disabled(monkeypatch):
    monkeypatch.setattr(api.config.release_execution, "allowed_services", ("test-api",))
    monkeypatch.setattr(
        api.config.release_execution, "remote_activation_enabled", False
    )
    with pytest.raises(policy.LocalReleasePolicyError, match="Actions dispatch"):
        policy.require_legacy_allowed("test-api")
    policy.require_legacy_allowed("other-api")


@pytest.mark.parametrize(
    "state,busy,allowed",
    [
        ("active", False, False),
        ("disabled_manually", True, False),
        ("disabled_manually", False, True),
    ],
)
def test_publication_requires_exclusive_publisher(monkeypatch, state, busy, allowed):
    monkeypatch.setattr(api.config, "mock_mode", False)
    workflow = SimpleNamespace(
        state=state, get_runs=lambda **_kwargs: [object()] if busy else []
    )
    client = SimpleNamespace(
        get_repo=lambda _repo: SimpleNamespace(get_workflow=lambda _name: workflow)
    )
    monkeypatch.setattr(policy.github_deployments, "github_client", lambda: client)
    if allowed:
        policy.require_publication_handoff("test/repo")
    else:
        with pytest.raises(policy.LocalReleasePolicyError):
            policy.require_publication_handoff("test/repo")


def test_publication_handoff_unavailable_fails_closed(monkeypatch):
    monkeypatch.setattr(api.config, "mock_mode", False)

    def unavailable():
        raise OSError("offline")

    monkeypatch.setattr(policy.github_deployments, "github_client", unavailable)
    with pytest.raises(RuntimeError, match="Unable to verify"):
        policy.require_publication_handoff("test/repo")


def test_production_requires_durable_configuration(issuance, monkeypatch):
    monkeypatch.setattr(api.config.release_execution, "remote_activation_enabled", True)
    monkeypatch.setattr(api.config.release_execution, "allowed_services", ("test-api",))
    monkeypatch.setattr(api.config.release_execution, "firestore_collection", "")
    with pytest.raises(policy.LocalReleasePolicyError, match="not configured"):
        policy.require_enabled("test-api")


@pytest.fixture
def registration(monkeypatch):
    from datetime import timedelta
    from eng_platform_api.routers import releases
    from eng_platform_api.models import ReleaseExecutionContext
    from scripts.release.lifecycle_control import effect_digest
    from scripts.release.local_release import digest

    monkeypatch.setattr(api.config, "mock_mode", False)
    monkeypatch.setattr(releases, "require_deployer", lambda _request: "operator")
    monkeypatch.setattr(policy, "require_enabled", lambda _service: None)
    payload = {
        "release_id": "individual",
        "repository": "test/repo",
        "version": "v1.0.0",
        "source_sha": "a" * 40,
        "artifact_digest": "sha256:" + "b" * 64,
        "status": "candidate",
        "services": [
            {"service_name": "test-api", "revision": "rev-1", "action": "deployed"}
        ],
    }
    context = ReleaseExecutionContext(
        release_id=payload["release_id"],
        repository=payload["repository"],
        source_sha=payload["source_sha"],
        artifact_digest=payload["artifact_digest"],
        tag=payload["version"],
        service_name="test-api",
        operation="register",
        actor_id="operator",
        target="project/region/test-api",
        configuration_hash="c" * 64,
    )
    scope = "deployment:project/region/test-api"
    intent = SimpleNamespace(
        context=context,
        status="INTENDED",
        scope_key=scope,
        lease_id="lease-1",
        lease_generation=1,
        effect_digest=effect_digest(
            {
                "context": context.model_dump(),
                "scope_key": scope,
                "stage": "register",
                "effect_key": "platform-registration:candidate",
                "command": ["POST", "http://testserver/api/releases/", digest(payload)],
            }
        ),
    )
    lease = SimpleNamespace(
        status="HELD",
        lease_id="lease-1",
        generation=1,
        expires_at=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    )
    monkeypatch.setattr(
        releases.execution_control_store, "get_intent", lambda _id: intent
    )
    monkeypatch.setattr(
        releases.execution_control_store, "get_lease", lambda _scope: lease
    )
    saved = []
    monkeypatch.setattr(
        releases.releases_store, "save_release", lambda value: saved.append(value) or []
    )
    return SimpleNamespace(
        client=TestClient(app), payload=payload, intent=intent, lease=lease, saved=saved
    )


def test_registration_requires_intent(registration):
    assert (
        registration.client.post(
            "/api/releases/", json=registration.payload
        ).status_code
        == 409
    )
    assert not registration.saved


def test_registration_rejects_anonymous_session(monkeypatch):
    monkeypatch.setattr(api.config, "mock_mode", False)
    response = TestClient(app).post(
        "/api/releases/",
        json={
            "release_id": "local-release",
            "repository": "test/repo",
            "version": "v1.0.0",
            "services": [{"service_name": "test-api", "revision": "rev-1"}],
        },
    )
    assert response.status_code == 401


def test_registration_accepts_exact_authorized_payload(registration):
    response = registration.client.post(
        "/api/releases/",
        json=registration.payload,
        headers={"X-Release-Intent": "intent-1"},
    )
    assert response.status_code == 201, response.text
    assert len(registration.saved) == 1


@pytest.mark.parametrize(
    "change",
    [
        "actor",
        "digest",
        "revision",
        "expired",
        "released",
        "generation",
        "intent_status",
    ],
)
def test_registration_rejects_changed_or_expired_intent(registration, change):
    if change == "actor":
        registration.intent.context.actor_id = "other"
    elif change == "digest":
        registration.payload["artifact_digest"] = "sha256:" + "d" * 64
    elif change == "revision":
        registration.payload["services"][0]["revision"] = "rev-other"
    elif change == "expired":
        registration.lease.expires_at = "2000-01-01T00:00:00+00:00"
    elif change == "released":
        registration.lease.status = "RELEASED"
    elif change == "generation":
        registration.lease.generation = 2
    else:
        registration.intent.status = "CONFIRMED"
    response = registration.client.post(
        "/api/releases/",
        json=registration.payload,
        headers={"X-Release-Intent": "intent-1"},
    )
    assert response.status_code in {403, 409}, response.text
    assert not registration.saved
