"""Local, synthetic tests for shared release execution control."""

from __future__ import annotations

import json
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from eng_platform_api.config import config
from eng_platform_api.models import (
    ExecutionIntentCreateRequest,
    ExecutionIntentReconcileRequest,
    ExecutionIntentResultRequest,
    ExecutionLeaseAcquireRequest,
    ExecutionLeaseReconcileRequest,
    ExecutionLeaseReleaseRequest,
    ExecutionLeaseRenewRequest,
    ReleaseExecutionContext,
)
from eng_platform_api.services import (
    execution_control_store as store,
    release_authorization,
    release_authorization_store,
)
from eng_platform_api.routers import release_execution as execution_router
from eng_platform_api.models import ReleaseExecutionAuthorizationIssueRequest
from scripts.release.execution_control import (
    ExecutionContext,
    ExecutionControlError,
    PlatformExecutionControlClient,
)
from scripts.release import release_lifecycle
from scripts.release.lifecycle_control import (
    LifecycleControlSession,
    LifecycleControlError,
)


SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
EFFECT_DIGEST = "c" * 64
OTHER_EFFECT_DIGEST = "e" * 64
OBSERVATION_DIGEST = "d" * 64


@pytest.fixture(autouse=True)
def isolated_control_store(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_DEFAULT_STORE_PATH", tmp_path / "control.json")
    monkeypatch.setattr(store, "_COLLECTION", "")
    monkeypatch.setattr(release_authorization_store, "_mock_entries", {})
    monkeypatch.setattr(config, "mock_mode", True)
    monkeypatch.setattr(config.release_execution, "remote_activation_enabled", True)
    monkeypatch.setattr(config.release_execution, "allowed_services", ("example",))
    monkeypatch.setattr(config.auth, "allowed_logins", ("diegomad14",))
    key = Ed25519PrivateKey.generate()
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(config.github, "release_signing_private_key", private_pem)
    monkeypatch.setattr(config.github, "release_signing_public_key", "")


@pytest.fixture
def api_client():
    from eng_platform_api.main import app

    return TestClient(app)


def _context(**overrides) -> ExecutionContext:
    values = {
        "release_id": "release-local-001",
        "repository": "diegomad14/example",
        "source_sha": SHA,
        "tag": "v1.2.3",
        "operation": "candidate",
        "actor_id": "diegomad14",
        "service_name": "example",
        "artifact_digest": DIGEST,
        "target": "example/prod",
        "configuration_hash": "f" * 64,
    }
    values.update(overrides)
    return ExecutionContext(**values)


def _issue_local_authorization(context: ExecutionContext, **overrides):
    values = {
        "repository": context.repository,
        "service_name": "example",
        "tag": context.tag,
        "sha": context.source_sha,
        "github_deployment_id": 0,
        "requested_by": context.actor_id,
        "kind": "deploy",
        "release_id": context.release_id,
        "artifact_digest": context.artifact_digest,
        "target": context.target,
        "operation": context.operation,
        "audience": release_authorization.LOCAL_AUDIENCE,
        "execution_mode": release_authorization.LOCAL_EXECUTION_MODE,
        "configuration": {"target": context.target},
    }
    values.update(overrides)
    token, claims = release_authorization.issue(**values)
    return replace(
        context, configuration_hash=claims.get("configuration_hash", "")
    ), token


def _transport(api_client):
    def send(path, payload):
        response = api_client.post(path, json=payload)
        if response.status_code >= 400:
            detail = response.json().get("detail", "")
            raise ExecutionControlError(
                f"Control plane rejected {path} ({response.status_code}): {detail}"
            )
        return response.json()

    return send


def test_capability_issue_derives_actor_from_authenticated_identity(
    api_client, monkeypatch
):
    """The issuance payload has no actor field and the signed claim is server-derived."""
    monkeypatch.setattr(
        "eng_platform_api.routers.release_execution._validate_capability",
        lambda _payload: None,
    )
    response = api_client.post(
        "/api/internal/release-execution/authorizations/issue",
        json={
            "release_id": "release-local-001",
            "repository": "diegomad14/example",
            "service_name": "example",
            "source_sha": SHA,
            "tag": "v1.2.3",
            "artifact_digest": DIGEST,
            "target": "project/region/example",
            "operation": "candidate",
            "configuration_hash": "f" * 64,
        },
    )
    assert response.status_code == 200
    claims = release_authorization.verify(
        response.json()["token"], {}, audience=release_authorization.LOCAL_AUDIENCE
    )
    assert claims["requested_by"] == "diegomad14"
    assert claims["capability_issued"] is True


def test_publish_capability_uses_repository_oss_v2_destination(monkeypatch):
    service = SimpleNamespace(
        repository="diegomad14/example",
        project_id="project",
        region="region",
        service_name="example",
        deployment_ready=True,
        release_policy="oss-v2",
        quality=SimpleNamespace(policy_version="oss-v2"),
    )
    report = SimpleNamespace(
        repository=service.repository,
        commit_sha=SHA,
        policy_version="oss-v2",
        quality_gate_status="PASSED",
    )
    monkeypatch.setattr(execution_router.catalog, "get_service", lambda _name: service)
    monkeypatch.setattr(
        execution_router, "get_quality_report", lambda *_args, **_kwargs: report
    )
    execution_router._validate_capability(
        ReleaseExecutionAuthorizationIssueRequest(
            release_id="release-local-001",
            repository=service.repository,
            service_name=service.service_name,
            source_sha=SHA,
            tag="v1.2.3",
            artifact_digest=DIGEST,
            target=f"{service.repository}:oss-v2",
            operation="publish",
            configuration_hash="f" * 64,
        )
    )


def test_control_consumption_rejects_payload_actor_not_owned_by_session(api_client):
    context, token = _issue_local_authorization(_context())
    response = api_client.post(
        "/api/internal/release-execution/authorizations/consume",
        json={"token": token, **context.as_payload(), "actor_id": "other-user"},
    )
    assert response.status_code == 403


def _authorized_record(context: ExecutionContext, jti: str) -> None:
    release_authorization_store._mock_entries[jti] = {
        "authorization_mode": "local-cli",
        "actor_id": context.actor_id,
        "requested_by": context.actor_id,
        "release_id": context.release_id,
        "repository": context.repository,
        "service_name": "example",
        "release_group_id": "",
        "source_sha": context.source_sha,
        "tag": context.tag,
        "artifact_digest": context.artifact_digest,
        "target": context.target,
        "operation": context.operation,
        "configuration_hash": context.configuration_hash,
        "exp": int(time.time()) + 300,
    }


def _lease_request(context: ExecutionContext, **overrides):
    values = {
        **context.as_payload(),
        "scope": "deployment",
        "scope_key": "deployment:example:prod",
        "owner_id": "cli-one",
        "authorization_jti": "auth-1",
        "ttl_seconds": 60,
    }
    values.update(overrides)
    return ExecutionLeaseAcquireRequest(**values)


def test_lifecycle_replayed_intent_never_grants_a_second_effect(api_client):
    context, token = _issue_local_authorization(_context())
    client = PlatformExecutionControlClient("", transport=_transport(api_client))
    first = LifecycleControlSession.open(
        client,
        context,
        token=token,
        owner_id="same-owner",
        scope="deployment",
        scope_key="deployment:example:prod",
    )
    second = replace(first)
    entered, finish = threading.Event(), threading.Event()
    effects = []

    def effect():
        effects.append("effect")
        entered.set()
        assert finish.wait(10)
        return "done"

    def execute(session):
        return session.run_effect(
            execution={},
            stage="candidate",
            intent="deploy",
            effect_key="deploy",
            effect=effect,
            emit=lambda *_a, **_k: None,
            persist=lambda: None,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        running = pool.submit(execute, first)
        try:
            assert entered.wait(10)
            with pytest.raises(LifecycleControlError, match="already exists"):
                execute(second)
        finally:
            finish.set()
        assert running.result() == "done"
    with pytest.raises(LifecycleControlError, match="already exists"):
        execute(replace(first))
    assert effects == ["effect"]
    first.finish(final_status="CONFIRMED")
    with pytest.raises(LifecycleControlError, match="closed"):
        execute(first)


def test_other_resource_and_released_owner_cannot_record_intent_result():
    context = _context()
    _authorized_record(context, "auth-1")
    lease = store.acquire_lease(_lease_request(context))
    _authorized_record(context, "auth-other")
    other = store.acquire_lease(
        _lease_request(
            context, scope_key="deployment:other:prod", authorization_jti="auth-other"
        )
    )
    intent = store.create_intent(
        ExecutionIntentCreateRequest(
            **context.as_payload(),
            idempotency_key="bound-result",
            effect_digest=EFFECT_DIGEST,
            scope=lease.scope,
            scope_key=lease.scope_key,
            owner_id=lease.owner_id,
            lease_id=lease.lease_id,
            lease_generation=lease.generation,
            authorization_jti="auth-1",
        )
    )

    def result_for(owner):
        return ExecutionIntentResultRequest(
            intent_id=intent.intent_id,
            scope_key=owner.scope_key,
            owner_id=owner.owner_id,
            lease_id=owner.lease_id,
            lease_generation=owner.generation,
            lease_version=owner.version,
            status="UNKNOWN",
        )

    with pytest.raises(store.StaleOwner):
        store.record_intent_result(result_for(other))
    store.release_lease(
        ExecutionLeaseReleaseRequest(
            lease_id=lease.lease_id,
            scope_key=lease.scope_key,
            owner_id=lease.owner_id,
            generation=lease.generation,
            version=lease.version,
            final_status="FAILED",
        )
    )
    with pytest.raises(store.StaleOwner):
        store.record_intent_result(result_for(lease))
    assert store.get_intent(intent.intent_id).status == "INTENDED"


def test_reconcile_rejects_lease_version_before_renewal():
    context = _context()
    _authorized_record(context, "auth-1")
    lease = store.acquire_lease(_lease_request(context))
    store.renew_lease(
        ExecutionLeaseRenewRequest(
            lease_id=lease.lease_id,
            scope_key=lease.scope_key,
            owner_id=lease.owner_id,
            generation=lease.generation,
            version=lease.version,
        )
    )
    with pytest.raises(store.StaleOwner):
        store.reconcile_lease(
            ExecutionLeaseReconcileRequest(
                lease_id=lease.lease_id,
                scope_key=lease.scope_key,
                owner_id=lease.owner_id,
                generation=lease.generation,
                version=lease.version,
                reconciliation_id="stale-version",
                observation="NOT_STARTED",
                observation_digest=OBSERVATION_DIGEST,
            )
        )


def test_renewed_owner_can_reconcile_intent_created_before_renewal(api_client):
    context, token = _issue_local_authorization(_context())
    client = PlatformExecutionControlClient("", transport=_transport(api_client))
    session = LifecycleControlSession.open(
        client,
        context,
        token=token,
        owner_id="owner",
        scope="deployment",
        scope_key="deployment:example:prod",
    )
    intent = client.create_intent(
        context,
        idempotency_key="renewed-intent",
        effect_digest=EFFECT_DIGEST,
        lease=session.lease,
        authorization_jti=session.authorization_jti,
    )
    renewed = client.renew_lease(session.lease, ttl_seconds=60)
    client.record_result(intent, lease=renewed, status="UNKNOWN")
    with pytest.raises(ExecutionControlError, match="409"):
        client.reconcile_intent(
            intent,
            reconciliation_id="stale",
            outcome="CONFIRMED",
            observation_digest=OBSERVATION_DIGEST,
        )
    reconciled = client.reconcile_intent(
        intent,
        lease=renewed,
        reconciliation_id="renewed",
        outcome="CONFIRMED",
        observation_digest=OBSERVATION_DIGEST,
    )
    assert reconciled.status == "CONFIRMED"
    assert store.get_lease(renewed.scope_key).status == "RELEASED"


def test_two_independent_clients_share_lease_and_intent_contract(api_client):
    context, token = _issue_local_authorization(_context())
    cli = PlatformExecutionControlClient("", transport=_transport(api_client))
    actions = PlatformExecutionControlClient("", transport=_transport(api_client))

    consumed = cli.consume_authorization(token, context)
    lease = cli.acquire_lease(
        context,
        scope="deployment",
        scope_key="deployment:example:prod",
        owner_id="cli-one",
        authorization_jti=consumed["jti"],
        ttl_seconds=60,
    )
    assert store.get_lease(lease.scope_key).context.service_name == "example"
    assert store.get_lease(lease.scope_key).context.release_group_id == ""
    with pytest.raises(ExecutionControlError, match="409"):
        actions.acquire_lease(
            context,
            scope="deployment",
            scope_key=lease.scope_key,
            owner_id="actions-one",
            authorization_jti=consumed["jti"],
            ttl_seconds=60,
        )

    intent = cli.create_intent(
        context,
        idempotency_key="release-local-001:candidate",
        effect_digest=EFFECT_DIGEST,
        lease=lease,
        authorization_jti=consumed["jti"],
    )
    same_intent = actions.create_intent(
        context,
        idempotency_key=intent.intent_id,
        effect_digest=EFFECT_DIGEST,
        lease=lease,
        authorization_jti=consumed["jti"],
    )
    assert same_intent.intent_id == intent.intent_id
    assert intent.created is True
    assert same_intent.created is False
    assert intent.context["service_name"] == "example"
    assert intent.context["release_group_id"] == ""
    with pytest.raises(ExecutionControlError, match="409"):
        actions.create_intent(
            context,
            idempotency_key=intent.intent_id,
            effect_digest=OTHER_EFFECT_DIGEST,
            lease=lease,
            authorization_jti=consumed["jti"],
        )

    unknown = cli.record_result(
        intent,
        lease=lease,
        status="UNKNOWN",
        error_code="response_lost",
    )
    assert unknown.status == "UNKNOWN"
    assert unknown.reconciliation_required is True
    with pytest.raises(ExecutionControlError, match="409"):
        cli.create_intent(
            context,
            idempotency_key="release-local-001:candidate-retry",
            effect_digest=EFFECT_DIGEST,
            lease=lease,
            authorization_jti=consumed["jti"],
        )
    reconciled = cli.reconcile_intent(
        unknown,
        reconciliation_id="reconcile-1",
        outcome="CONFIRMED",
        observation_digest=OBSERVATION_DIGEST,
    )
    assert reconciled.status == "CONFIRMED"
    assert store.get_lease(lease.scope_key).status == "RELEASED"


def test_concurrent_local_clients_have_one_atomic_winner():
    context = _context()
    _authorized_record(context, "auth-one")
    _authorized_record(context, "auth-two")
    requests = [
        _lease_request(context, owner_id="cli-one", authorization_jti="auth-one"),
        _lease_request(context, owner_id="actions-one", authorization_jti="auth-two"),
    ]

    def acquire(request):
        try:
            return store.acquire_lease(request)
        except Exception as exc:  # the result is asserted below, not discarded
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(acquire, requests))
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, store.LeaseConflict) for result in results) == 1


def test_authorization_requires_platform_ticket_and_rejects_reuse_or_tampering(
    api_client,
):
    context, token = _issue_local_authorization(_context())
    endpoint = "/api/internal/release-execution/authorizations/consume"
    first = api_client.post(endpoint, json={"token": token, **context.as_payload()})
    second = api_client.post(endpoint, json={"token": token, **context.as_payload()})
    assert first.status_code == 200
    assert second.status_code == 409

    token_parts = token.split(".")
    signature_head = "A" if token_parts[2][0] != "A" else "B"
    tampered = ".".join((*token_parts[:2], signature_head + token_parts[2][1:]))
    response = api_client.post(
        endpoint, json={"token": tampered, **context.as_payload()}
    )
    assert response.status_code == 401

    wrong_context = replace(context, target="example/other")
    _, token = _issue_local_authorization(context)
    response = api_client.post(
        endpoint, json={"token": token, **wrong_context.as_payload()}
    )
    assert response.status_code == 401

    github_token, _ = release_authorization.issue(
        repository=context.repository,
        service_name="example",
        tag=context.tag,
        sha=context.source_sha,
        github_deployment_id=0,
        requested_by=context.actor_id,
        kind="deploy",
    )
    response = api_client.post(
        endpoint, json={"token": github_token, **context.as_payload()}
    )
    assert response.status_code == 401


def test_authorization_rejects_wrong_mode_and_reports_store_failure(
    api_client, monkeypatch
):
    context = _context()
    _, token = _issue_local_authorization(context, execution_mode="github-actions")
    endpoint = "/api/internal/release-execution/authorizations/consume"
    response = api_client.post(endpoint, json={"token": token, **context.as_payload()})
    assert response.status_code == 401

    context, token = _issue_local_authorization(_context(release_id="release-002"))
    monkeypatch.setattr(
        release_authorization_store,
        "consume",
        mock.Mock(side_effect=RuntimeError("store unavailable")),
    )
    response = api_client.post(endpoint, json={"token": token, **context.as_payload()})
    assert response.status_code == 503


def test_expired_platform_authorization_is_rejected(api_client):
    context, token = _issue_local_authorization(_context())
    endpoint = "/api/internal/release-execution/authorizations/consume"
    with mock.patch.object(
        release_authorization.time,
        "time",
        return_value=int(time.time()) + release_authorization.TOKEN_TTL_SECONDS + 1,
    ):
        response = api_client.post(
            endpoint, json={"token": token, **context.as_payload()}
        )
    assert response.status_code == 401


def test_expired_lease_requires_reconciliation_and_rejects_stale_owner(monkeypatch):
    now = [datetime(2026, 9, 9, 20, 0, tzinfo=timezone.utc)]
    monkeypatch.setattr(store, "_now", lambda: now[0])
    context = _context()
    _authorized_record(context, "auth-1")
    _authorized_record(context, "auth-2")
    first = store.acquire_lease(_lease_request(context))
    intent = store.create_intent(
        ExecutionIntentCreateRequest(
            **context.as_payload(),
            idempotency_key="expired-intent",
            effect_digest=EFFECT_DIGEST,
            scope=first.scope,
            scope_key=first.scope_key,
            owner_id=first.owner_id,
            lease_id=first.lease_id,
            lease_generation=first.generation,
            authorization_jti="auth-1",
        )
    )
    with pytest.raises(store.AuthorizationRequired, match="reused"):
        store.acquire_lease(
            _lease_request(
                context,
                scope="publication",
                scope_key="publication:diegomad14/example:oss-v2",
            )
        )
    now[0] += timedelta(seconds=61)

    with pytest.raises(store.StaleOwner, match="UNKNOWN"):
        store.record_intent_result(
            ExecutionIntentResultRequest(
                intent_id=intent.intent_id,
                scope_key=first.scope_key,
                owner_id=first.owner_id,
                lease_id=first.lease_id,
                lease_generation=first.generation,
                status="CONFIRMED",
            )
        )
    assert (
        store.record_intent_result(
            ExecutionIntentResultRequest(
                intent_id=intent.intent_id,
                scope_key=first.scope_key,
                owner_id=first.owner_id,
                lease_id=first.lease_id,
                lease_generation=first.generation,
                status="UNKNOWN",
            )
        ).status
        == "UNKNOWN"
    )

    with pytest.raises(store.LeaseTakeoverBlocked):
        store.acquire_lease(
            _lease_request(
                context,
                owner_id="actions-one",
                authorization_jti="auth-2",
            )
        )
    indeterminate = store.reconcile_lease(
        ExecutionLeaseReconcileRequest(
            lease_id=first.lease_id,
            scope_key=first.scope_key,
            owner_id=first.owner_id,
            generation=first.generation,
            reconciliation_id="reconcile-unknown",
            observation="INDETERMINATE",
            observation_digest=OBSERVATION_DIGEST,
        )
    )
    assert indeterminate.takeover_allowed is False
    with pytest.raises(store.LeaseTakeoverBlocked):
        store.acquire_lease(
            _lease_request(
                context,
                owner_id="actions-one",
                authorization_jti="auth-2",
                reconciliation_id="reconcile-unknown",
            )
        )

    safe = store.reconcile_lease(
        ExecutionLeaseReconcileRequest(
            lease_id=first.lease_id,
            scope_key=first.scope_key,
            owner_id=first.owner_id,
            generation=first.generation,
            reconciliation_id="reconcile-safe",
            observation="NOT_STARTED",
            observation_digest=OBSERVATION_DIGEST,
        )
    )
    assert safe.takeover_allowed is True
    second = store.acquire_lease(
        _lease_request(
            context,
            owner_id="actions-one",
            authorization_jti="auth-2",
            reconciliation_id="reconcile-safe",
        )
    )
    assert second.generation == first.generation + 1
    complete = store.reconcile_lease(
        ExecutionLeaseReconcileRequest(
            lease_id=second.lease_id,
            scope_key=second.scope_key,
            owner_id=second.owner_id,
            generation=second.generation,
            reconciliation_id="reconcile-complete",
            observation="COMPLETE",
            observation_digest=OBSERVATION_DIGEST,
        )
    )
    assert complete.takeover_allowed is False
    with pytest.raises(store.LeaseTakeoverBlocked):
        store.acquire_lease(
            _lease_request(
                context,
                owner_id="cli-three",
                authorization_jti="auth-2",
                reconciliation_id="reconcile-complete",
            )
        )
    with pytest.raises(store.StaleOwner):
        store.renew_lease(
            ExecutionLeaseRenewRequest(
                lease_id=first.lease_id,
                scope_key=first.scope_key,
                owner_id=first.owner_id,
                generation=first.generation,
                version=first.version,
                ttl_seconds=60,
            )
        )
    with pytest.raises(store.StaleOwner):
        store.release_lease(
            ExecutionLeaseReleaseRequest(
                lease_id=first.lease_id,
                scope_key=first.scope_key,
                owner_id=first.owner_id,
                generation=first.generation,
                version=first.version,
                ttl_seconds=60,
                final_status="CONFIRMED",
            )
        )


def test_renew_release_idempotency_and_file_persistence(monkeypatch, tmp_path):
    context = _context()
    _authorized_record(context, "auth-1")
    lease = store.acquire_lease(_lease_request(context))
    renewed = store.renew_lease(
        ExecutionLeaseRenewRequest(
            lease_id=lease.lease_id,
            scope_key=lease.scope_key,
            owner_id=lease.owner_id,
            generation=lease.generation,
            version=lease.version,
            ttl_seconds=60,
        )
    )
    assert renewed.version == lease.version + 1
    with pytest.raises(store.StaleOwner):
        store.renew_lease(
            ExecutionLeaseRenewRequest(
                lease_id=renewed.lease_id,
                scope_key=renewed.scope_key,
                owner_id=renewed.owner_id,
                generation=renewed.generation,
                version=lease.version,
                ttl_seconds=60,
            )
        )
    released = store.release_lease(
        ExecutionLeaseReleaseRequest(
            lease_id=renewed.lease_id,
            scope_key=renewed.scope_key,
            owner_id=renewed.owner_id,
            generation=renewed.generation,
            version=renewed.version,
            ttl_seconds=60,
            final_status="FAILED",
        )
    )
    assert (
        store.release_lease(
            ExecutionLeaseReleaseRequest(
                lease_id=released.lease_id,
                scope_key=released.scope_key,
                owner_id=released.owner_id,
                generation=released.generation,
                version=released.version,
                ttl_seconds=60,
                final_status="FAILED",
            )
        ).status
        == "RELEASED"
    )
    assert (tmp_path / "control.json").exists()

    with mock.patch.object(store, "_DEFAULT_STORE_PATH", tmp_path / "control.json"):
        assert store.get_lease(lease.scope_key).final_status == "FAILED"


def test_intent_requires_active_lease_and_result_conflicts():
    context = _context()
    _authorized_record(context, "auth-1")
    lease = store.acquire_lease(_lease_request(context))
    request = ExecutionIntentCreateRequest(
        **context.as_payload(),
        idempotency_key="release-local-001:publish",
        effect_digest=EFFECT_DIGEST,
        scope=lease.scope,
        scope_key=lease.scope_key,
        owner_id=lease.owner_id,
        lease_id=lease.lease_id,
        lease_generation=lease.generation,
        authorization_jti="auth-1",
    )
    intent = store.create_intent(request)
    same = store.create_intent(request)
    assert same.intent_id == intent.intent_id
    result = store.record_intent_result(
        ExecutionIntentResultRequest(
            intent_id=intent.intent_id,
            scope_key=lease.scope_key,
            owner_id=lease.owner_id,
            lease_id=lease.lease_id,
            lease_generation=lease.generation,
            status="CONFIRMED",
            result_digest=EFFECT_DIGEST,
        )
    )
    assert result.status == "CONFIRMED"
    with pytest.raises(store.IntentConflict):
        store.record_intent_result(
            ExecutionIntentResultRequest(
                intent_id=intent.intent_id,
                scope_key=lease.scope_key,
                owner_id=lease.owner_id,
                lease_id=lease.lease_id,
                lease_generation=lease.generation,
                status="FAILED",
            )
        )
    with pytest.raises(store.AuthorizationRequired):
        store.create_intent(
            request.model_copy(
                update={
                    "idempotency_key": "missing-auth-intent",
                    "authorization_jti": "missing-auth",
                }
            )
        )


def test_models_reject_unbound_digests_and_bad_result():
    with pytest.raises(ValueError):
        ReleaseExecutionContext(**_context(artifact_digest="not-a-digest").as_payload())
    with pytest.raises(ValueError):
        ReleaseExecutionContext(
            **_context(configuration_hash="not-a-hash").as_payload()
        )
    with pytest.raises(ValueError):
        ExecutionIntentResultRequest(
            intent_id="intent",
            scope_key="scope",
            owner_id="owner",
            lease_id="lease",
            lease_generation=1,
            status="CONFIRMED",
            result_digest="not-a-hash",
        )
    with pytest.raises(ValueError, match="scope_key"):
        ExecutionLeaseAcquireRequest(
            **_context().as_payload(),
            scope="publication",
            scope_key="deployment:wrong",
            owner_id="owner",
            authorization_jti="auth",
        )


def test_firestore_adapter_path_uses_transaction_and_existing_persistence(monkeypatch):
    class Snapshot:
        def __init__(self, data=None):
            self.data = data
            self.exists = data is not None

        def to_dict(self):
            return self.data

    class Document:
        def __init__(self, records, key):
            self.records = records
            self.key = key

        def get(self, transaction=None):
            return Snapshot(self.records.get(self.key))

    class Collection:
        def __init__(self):
            self.records = {}

        def document(self, key):
            return Document(self.records, key)

    class Transaction:
        def set(self, document, value):
            document.records[document.key] = value

    collection = Collection()
    transaction = Transaction()
    monkeypatch.setattr(store, "_firestore_collection", lambda: collection)
    monkeypatch.setattr(
        store, "_firestore_transaction", lambda _c, callback: callback(transaction)
    )
    context = _context()
    _authorized_record(context, "auth-1")
    lease = store.acquire_lease(_lease_request(context))
    assert store.get_lease(lease.scope_key).lease_id == lease.lease_id
    intent = store.create_intent(
        ExecutionIntentCreateRequest(
            **context.as_payload(),
            idempotency_key="firestore-intent",
            effect_digest=EFFECT_DIGEST,
            scope=lease.scope,
            scope_key=lease.scope_key,
            owner_id=lease.owner_id,
            lease_id=lease.lease_id,
            lease_generation=lease.generation,
            authorization_jti="auth-1",
        )
    )
    assert store.get_intent(intent.intent_id).status == "INTENDED"
    assert (
        store.record_intent_result(
            ExecutionIntentResultRequest(
                intent_id=intent.intent_id,
                scope_key=lease.scope_key,
                owner_id=lease.owner_id,
                lease_id=lease.lease_id,
                lease_generation=lease.generation,
                status="FAILED",
                error_code="simulated",
            )
        ).status
        == "FAILED"
    )


def test_firestore_client_selection_and_http_client_failures(monkeypatch):
    fake_collection = object()
    monkeypatch.setattr(store, "_COLLECTION", "release-control")
    monkeypatch.setattr(
        "eng_platform_api.services.deployment_store.firestore_client",
        lambda _project: mock.Mock(collection=lambda _name: fake_collection),
    )
    assert store._firestore_collection() is fake_collection
    monkeypatch.setattr(store, "_COLLECTION", "")

    client = PlatformExecutionControlClient("", auth_headers={})
    with pytest.raises(ExecutionControlError, match="URL"):
        client._post("/path", {})

    bad_transport = PlatformExecutionControlClient(
        "", transport=lambda _path, _payload: []
    )
    with pytest.raises(ExecutionControlError, match="non-object"):
        bad_transport._post("/path", {})

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps({"ok": True}).encode()

    monkeypatch.setattr(
        "scripts.release.execution_control.authenticated_urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    assert PlatformExecutionControlClient("https://control", auth_headers={})._post(
        "/path", {}
    ) == {"ok": True}


def test_http_client_reports_rejection_invalid_json_and_unavailable(monkeypatch):
    from urllib.error import HTTPError

    client = PlatformExecutionControlClient("https://control", auth_headers={})

    def rejected(*_args, **_kwargs):
        raise HTTPError(
            "http://control/path",
            409,
            "conflict",
            {},
            mock.Mock(read=lambda: b'{"detail":"scope busy"}'),
        )

    monkeypatch.setattr(
        "scripts.release.execution_control.authenticated_urlopen", rejected
    )
    with pytest.raises(ExecutionControlError, match="409"):
        client._post("/path", {})

    class InvalidResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b"not-json"

    monkeypatch.setattr(
        "scripts.release.execution_control.authenticated_urlopen",
        lambda *_args, **_kwargs: InvalidResponse(),
    )
    with pytest.raises(ExecutionControlError, match="invalid JSON"):
        client._post("/path", {})

    monkeypatch.setattr(
        "scripts.release.execution_control.authenticated_urlopen",
        mock.Mock(side_effect=OSError("offline")),
    )
    with pytest.raises(ExecutionControlError, match="unavailable"):
        client._post("/path", {})


def test_lifecycle_exposes_contract_but_keeps_remote_activation_closed():
    plan = release_lifecycle.live_control_plan(
        {"service_name": "example", "dependencies": {"workflow_inventory": {}}},
        "candidate",
    )
    assert plan["adapter_contract"]["status"] == "IMPLEMENTED"
    assert plan["remote_activation"] == {
        "enabled": False,
        "cli_override_supported": False,
        "authority": "Engineering Platform authenticated per-service authorization",
    }
    assert not hasattr(release_lifecycle, "REMOTE_ACTIVATION_ENABLED")


def test_reconcile_intent_unknown_is_not_a_retry():
    context = _context()
    _authorized_record(context, "auth-1")
    lease = store.acquire_lease(_lease_request(context))
    intent = store.create_intent(
        ExecutionIntentCreateRequest(
            **context.as_payload(),
            idempotency_key="unknown-intent",
            effect_digest=EFFECT_DIGEST,
            scope=lease.scope,
            scope_key=lease.scope_key,
            owner_id=lease.owner_id,
            lease_id=lease.lease_id,
            lease_generation=lease.generation,
            authorization_jti="auth-1",
        )
    )
    unknown = store.record_intent_result(
        ExecutionIntentResultRequest(
            intent_id=intent.intent_id,
            scope_key=lease.scope_key,
            owner_id=lease.owner_id,
            lease_id=lease.lease_id,
            lease_generation=lease.generation,
            status="UNKNOWN",
        )
    )
    reconciled = store.reconcile_intent(
        ExecutionIntentReconcileRequest(
            intent_id=unknown.intent_id,
            reconciliation_id="observation-1",
            outcome="UNKNOWN",
            scope_key=lease.scope_key,
            owner_id=lease.owner_id,
            lease_id=lease.lease_id,
            lease_generation=lease.generation,
            lease_version=lease.version,
            observation_digest=OBSERVATION_DIGEST,
        )
    )
    assert reconciled.status == "UNKNOWN"
    assert reconciled.reconciliation_required is True
    assert (
        store.create_intent(
            ExecutionIntentCreateRequest(
                **context.as_payload(),
                idempotency_key=unknown.intent_id,
                effect_digest=EFFECT_DIGEST,
                scope=lease.scope,
                scope_key=lease.scope_key,
                owner_id=lease.owner_id,
                lease_id=lease.lease_id,
                lease_generation=lease.generation,
                authorization_jti="auth-1",
            )
        ).status
        == "UNKNOWN"
    )
