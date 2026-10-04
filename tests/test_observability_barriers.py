"""Inventory admission never grants any management, persistence or token path.

Every observed identity is exercised with synthetic callers and forbidden cloud
clients. The only opaque-ID reads allowed are synthetic existing-record lookups.
"""

from datetime import datetime, timezone
from copy import deepcopy
from unittest.mock import Mock

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from eng_platform_api.config import config
from eng_platform_api.main import app
from eng_platform_api.models import (
    CatalogService,
    DeploymentItem,
    OperationalSecret,
    QualityReportCreate,
    ReleaseCreateRequest,
    ReleaseTag,
)
from eng_platform_api.routers import release_execution_events as events
from eng_platform_api.services import (
    cloud_build,
    deployment_commands,
    deployment_store,
    github_deployments,
    github_release_control,
    log_catalog,
    operational_secrets,
    quality_store,
    release_authorization,
    release_authorization_store,
    release_cloud_build,
    release_executions,
    release_orchestrator,
    release_reconciler,
    releases_store,
    resource_access,
)

OBSERVATIONS = [
    row
    # Import-time parametrization reads only the explicit public synthetic file,
    # never the production runtime-source selector before fixtures are applied.
    for row in log_catalog.load_catalog(log_catalog.CATALOG_PATH)
    if row.get("management_mode") == "observability_only"
]
NAMES = [row["service_name"] for row in OBSERVATIONS]


def report(name):
    return QualityReportCreate(
        service_name=name,
        repository="synthetic/repo",
        commit_sha="a" * 40,
        branch="main",
        profile="python",
        generated_at=datetime.now(timezone.utc).isoformat(),
        checks=[{"name": "tests", "category": "tests", "status": "PASSED"}],
    )


def item(name):
    return DeploymentItem(
        id="synthetic-deployment",
        service_name=name,
        repository="synthetic/repo",
        tag="v1.2.3",
        sha="a" * 40,
        github_deployment_id=123,
    )


@pytest.fixture
def no_cloud(monkeypatch, tmp_path):
    from tests.test_log_catalog import install_catalog, pin_private_catalog

    rows = deepcopy(OBSERVATIONS)
    for row in rows:
        row["logs"] = {"enabled": True, "allowed_logins": ["demo-reader"]}
    path = install_catalog(monkeypatch, tmp_path, rows)
    pin_private_catalog(monkeypatch, path)
    monkeypatch.setattr(config.logs, "allowed_logins", ("demo-reader",))
    monkeypatch.setattr(
        config.auth, "session_secret", "synthetic-session-secret-32-characters"
    )
    monkeypatch.setattr(config.auth, "github_client_id", "synthetic-client-id")
    monkeypatch.setattr(config.auth, "github_client_secret", "synthetic-client-secret")
    forbidden = Mock(
        side_effect=AssertionError("Observation attempted an external operation")
    )
    for module, names in [
        (github_deployments, ["github_client"]),
        (cloud_build, ["_session"]),
        (release_cloud_build, ["_session"]),
        (releases_store, ["_firestore_collection"]),
        (quality_store, ["_read_object", "_write_object"]),
        (operational_secrets, ["writer", "database"]),
        (
            deployment_store,
            ["get", "find_by_idempotency_key", "list_for_service", "save"],
        ),
        (
            release_executions,
            ["get", "save", "claim_publish", "claim_source_token", "claim_event_token"],
        ),
        (release_authorization_store, ["consume"]),
    ]:
        for name in names:
            monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(config, "mock_mode", False)
    yield forbidden
    forbidden.assert_not_called()


def denied(call):
    with pytest.raises(HTTPException) as error:
        call()
    assert error.value.status_code == 409
    assert "observation-only" in error.value.detail


def test_inventory_has_all_32_observation_only_resources():
    assert len(OBSERVATIONS) == 32
    assert (
        sum(
            row["deployment"]["runtime_kind"] == "cloud_run_service"
            for row in OBSERVATIONS
        )
        == 7
    )
    assert (
        sum(
            row["deployment"]["runtime_kind"] == "cloud_run_job" for row in OBSERVATIONS
        )
        == 25
    )


@pytest.mark.parametrize("row", OBSERVATIONS, ids=NAMES)
def test_all_observed_resources_block_management_sinks_before_external_calls(
    row, no_cloud
):
    service = CatalogService.model_validate(row)
    name = service.service_name
    deployment = item(name)
    tag = ReleaseTag(name="v1.2.3", sha="a" * 40)
    secret = OperationalSecret(key="SYNTHETIC", secret_id="synthetic")
    execution = {"service_name": name, "repository": "synthetic/repo"}
    calls = [
        lambda: resource_access.require_managed(service),
        lambda: deployment_commands.start_deployment(
            service_name=name, tag_name=tag.name, requested_by="operator"
        ),
        lambda: deployment_commands.start_rollback(
            service_name=name,
            target_deployment_id=deployment.id,
            requested_by="operator",
        ),
        lambda: deployment_commands.start_cloud_build(
            service, deployment, reason="test"
        ),
        lambda: github_deployments.start_deployment(
            service=service, tag=tag, requested_by="operator"
        ),
        lambda: github_deployments.start_rollback(
            service=service, target=deployment, requested_by="operator"
        ),
        lambda: github_deployments.start_managed_deployment(
            service=service, tag=tag, requested_by="operator"
        ),
        lambda: github_deployments.retry_dispatch(service=service, item=deployment),
        lambda: github_deployments.set_managed_status(
            deployment, state="queued", description="test"
        ),
        lambda: github_deployments.refresh(deployment),
        lambda: cloud_build.submit(deployment, service, reason="test"),
        lambda: cloud_build.refresh(deployment),
        lambda: release_cloud_build.submit("execution", service),
        lambda: release_cloud_build.reconcile_uncertain_submission(
            "execution", service
        ),
        lambda: github_release_control.publish_release(execution),
        lambda: release_reconciler._publish_if_allowed(execution),
        lambda: releases_store.save_release(
            ReleaseCreateRequest(
                repository="synthetic/repo",
                version="v1.2.3",
                services=[{"service_name": name}],
            )
        ),
        lambda: quality_store.save_report(report(name)),
        lambda: quality_store.save_pending_report("execution", report(name)),
        lambda: operational_secrets.metadata(service),
        lambda: operational_secrets.publish(
            service, secret, "synthetic", "operation", 0, "operator"
        ),
        lambda: operational_secrets.snapshot(service),
        lambda: operational_secrets.state(name),
        lambda: operational_secrets.finalize(None, name, "SYNTHETIC", "operation", "1"),
        lambda: release_authorization.issue(
            repository="synthetic/repo",
            service_name=name,
            tag="v1.2.3",
            sha="a" * 40,
            github_deployment_id=123,
            requested_by="operator",
            kind="deploy",
        ),
    ]
    for call in calls:
        denied(call)
    assert release_orchestrator._service_enabled(service) is False


@pytest.mark.parametrize("name", NAMES)
def test_all_observed_resource_http_mutations_are_blocked(name, no_cloud, monkeypatch):
    from eng_platform_api import security
    from tests.test_logs_api import client as log_client

    monkeypatch.setattr(security, "get_identity", lambda _: "operator")
    monkeypatch.setattr(config.auth, "allowed_logins", ("operator",))
    monkeypatch.setattr(config.auth, "frontend_url", "http://localhost:5173")
    monkeypatch.setenv("ENG_PLATFORM_QUALITY_INGEST_TOKEN", "synthetic-token")
    client = log_client("demo-reader")
    prefix = f"/api/services/{name}"
    responses = [
        client.post(prefix + "/deployments", json={"tag": "v1.2.3"}),
        client.post(prefix + "/deployments/synthetic/rollback"),
        client.get(prefix + "/secrets"),
        client.post(
            prefix + "/secrets/SYNTHETIC/versions",
            json={"value": "synthetic", "generation": 0},
            headers={
                "Origin": config.auth.frontend_url,
                "X-Requested-With": "EngineeringPlatform",
            },
        ),
        client.post(
            "/api/releases/",
            json={
                "repository": "synthetic/repo",
                "version": "v1.2.3",
                "services": [{"service_name": name}],
            },
        ),
        client.post(
            "/api/quality/reports",
            json=report(name).model_dump(mode="json"),
            headers={"Authorization": "Bearer synthetic-token"},
        ),
    ]
    for response in responses:
        assert response.status_code == 409, response.text
        assert "observation-only" in response.json()["detail"]


def test_stale_managed_object_cannot_restore_observed_name(no_cloud):
    # A caller cannot spoof managed status for a current inventory-only identity.
    stale = CatalogService.model_construct(
        service_name=NAMES[0],
        management_mode="managed",
        repository="synthetic/repo",
        owner="synthetic",
    )
    denied(lambda: resource_access.require_managed(stale))


def test_missing_management_metadata_is_rejected():
    invalid = CatalogService.model_construct(
        service_name="unknown-synthetic",
        management_mode="managed",
        repository=None,
        owner=None,
    )
    with pytest.raises(HTTPException) as error:
        resource_access.require_managed(invalid)
    assert error.value.status_code == 409


def test_corrupt_authority_fails_closed_before_management(monkeypatch, no_cloud):
    monkeypatch.setattr(
        log_catalog, "load_catalog", Mock(side_effect=log_catalog.CatalogUnavailable())
    )
    with pytest.raises(HTTPException) as error:
        deployment_commands.start_deployment(
            service_name="eng-platform-api", tag_name="v1.2.3", requested_by="operator"
        )
    assert error.value.status_code == 503


@pytest.mark.parametrize(
    "operation",
    ["reconcile", "approve", "supersede", "cancel", "source-token", "event-token"],
)
def test_opaque_execution_reads_block_before_mutation_or_token_issue(
    operation, monkeypatch, no_cloud
):
    execution = {
        "service_name": NAMES[0],
        "provider": "cloud_build",
        "fingerprint": "f" * 64,
        "operation": "pr_quality",
    }
    read = Mock(return_value=execution)
    monkeypatch.setattr(release_executions, "get", read)
    verified = Mock(return_value={"email": "synthetic@invalid"})
    monkeypatch.setattr(events, "_verify_google", verified)
    payload = events.SourceTokenRequest(fingerprint="f" * 64, provider_run_id="run")
    calls = {
        "reconcile": lambda: release_reconciler.reconcile("execution"),
        "approve": lambda: release_orchestrator.approve_canary(
            "execution", approved_by="operator"
        ),
        "supersede": lambda: release_orchestrator.supersede(
            "execution", superseded_by="operator", reason="test"
        ),
        "cancel": lambda: release_cloud_build.cancel("execution"),
        "source-token": lambda: events.issue_source_token(
            "execution", payload, "Bearer synthetic"
        ),
        "event-token": lambda: events.issue_event_token(
            "execution", payload, "Bearer synthetic"
        ),
    }
    denied(calls[operation])
    read.assert_called_once()
    if operation.endswith("token"):
        verified.assert_called_once()


def test_callback_authentication_still_precedes_management_barrier(
    monkeypatch, no_cloud
):
    execution = {
        "service_name": NAMES[0],
        "provider": "cloud_build",
        "fingerprint": "f" * 64,
    }
    monkeypatch.setattr(release_executions, "get", Mock(return_value=execution))
    monkeypatch.setattr(
        events,
        "_verify_google",
        Mock(side_effect=HTTPException(401, "Invalid identity")),
    )
    with pytest.raises(HTTPException) as error:
        events.issue_event_token(
            "execution",
            events.SourceTokenRequest(fingerprint="f" * 64, provider_run_id="run"),
            "Bearer invalid",
        )
    assert error.value.status_code == 401


def test_release_consumption_authentication_precedes_barrier(monkeypatch, no_cloud):
    monkeypatch.setattr(
        release_authorization,
        "verify",
        Mock(
            side_effect=release_authorization.ReleaseAuthorizationError("Invalid token")
        ),
    )
    response = TestClient(app).post(
        "/api/internal/release-authorizations/consume",
        json={
            "token": "invalid",
            "service_name": NAMES[0],
            "repository": "synthetic/repo",
            "tag": "v1.2.3",
            "sha": "a" * 40,
            "github_deployment_id": "123",
            "kind": "deploy",
        },
    )
    assert response.status_code == 401


def test_verified_release_authorization_cannot_be_consumed_for_observation(
    monkeypatch, no_cloud
):
    verified = Mock(return_value={"jti": "synthetic", "exp": 2_000_000_000})
    monkeypatch.setattr(release_authorization, "verify", verified)
    response = TestClient(app).post(
        "/api/internal/release-authorizations/consume",
        json={
            "token": "synthetic",
            "service_name": NAMES[0],
            "repository": "synthetic/repo",
            "tag": "v1.2.3",
            "sha": "a" * 40,
            "github_deployment_id": "123",
            "kind": "deploy",
        },
    )
    assert response.status_code == 409
    verified.assert_called_once()


def test_deployment_callback_authenticates_then_blocks_provider_and_persistence(
    monkeypatch, no_cloud
):
    from eng_platform_api.models import DeploymentExecutionEvent
    from eng_platform_api.routers import deployment_events

    verified = Mock()
    monkeypatch.setattr(deployment_events, "_verify_identity", verified)
    monkeypatch.setattr(
        deployment_events.deployment_executions,
        "get",
        Mock(return_value={"provider": "cloud_build"}),
    )
    monkeypatch.setattr(deployment_store, "get", Mock(return_value=item(NAMES[0])))
    provider = Mock(
        side_effect=AssertionError("Must not read a build for an observation")
    )
    monkeypatch.setattr(cloud_build, "get_build", provider)
    denied(
        lambda: deployment_events.accept_event(
            "synthetic",
            DeploymentExecutionEvent(
                build_id="synthetic",
                fingerprint="f" * 64,
                sequence=1,
                stage="promote",
                status="running",
            ),
            "Bearer synthetic",
        )
    )
    verified.assert_called_once_with("Bearer synthetic")
    provider.assert_not_called()


def test_execution_resolve_authenticates_before_blocking_updates(monkeypatch, no_cloud):
    execution = {"service_name": NAMES[0], "provider": "github_actions"}
    monkeypatch.setattr(release_executions, "find", Mock(return_value=execution))
    verified = Mock(return_value={"run_id": "1"})
    monkeypatch.setattr(events.release_workflow_identity, "verify", verified)
    denied(
        lambda: events.resolve_execution(
            events.ResolveExecutionRequest(
                repository="synthetic/repo",
                head_sha="a" * 40,
                operation="main_release",
            ),
            "Bearer synthetic",
        )
    )
    verified.assert_called_once()


def test_scheduler_does_not_turn_management_denial_into_a_record_write(
    monkeypatch, no_cloud
):
    from eng_platform_api.routers import release_operations

    monkeypatch.setattr(release_operations, "_verify_google", Mock())
    monkeypatch.setattr(
        release_operations.cloud_build_usage, "reconcile", Mock(return_value={})
    )
    monkeypatch.setattr(
        release_executions,
        "list_due",
        Mock(return_value=[{"execution_id": "synthetic"}]),
    )
    monkeypatch.setattr(
        release_executions, "get", Mock(return_value={"service_name": NAMES[0]})
    )
    monkeypatch.setattr(
        deployment_commands,
        "reconcile_stalled_dispatches",
        Mock(return_value={"reconciled": 0, "items": []}),
    )
    result = release_operations.reconcile_due("Bearer synthetic")
    assert result["items"] == [
        {"execution_id": "synthetic", "status": "management_blocked", "code": 409}
    ]


def test_stalled_deployment_sweep_skips_observation_before_execution_reads(
    monkeypatch, no_cloud
):
    monkeypatch.setattr(
        deployment_store, "list_unfinished", Mock(return_value=[item(NAMES[0])])
    )
    forbidden = Mock(side_effect=AssertionError("Observation must not be reconciled"))
    monkeypatch.setattr(deployment_commands.deployment_executions, "get", forbidden)
    assert deployment_commands.reconcile_stalled_dispatches() == {
        "reconciled": 0,
        "items": [],
    }
    forbidden.assert_not_called()


def test_quality_summary_skips_observations_without_inventing_repositories(
    monkeypatch, no_cloud
):
    from eng_platform_api.models import CatalogResponse
    from eng_platform_api.routers import quality

    service = CatalogService.model_validate(OBSERVATIONS[0])
    monkeypatch.setattr(
        quality.catalog,
        "get_services",
        Mock(return_value=CatalogResponse(services=[service], total=1)),
    )
    monkeypatch.setattr(quality, "_summary_cache", None)
    assert quality.get_quality_summary().projects == []
