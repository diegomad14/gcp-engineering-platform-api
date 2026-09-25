"""Shared, provider-neutral deployment commands.

Both REST and MCP call this module.  Keeping the command boundary here is
important: callers can authenticate differently, but cannot select an
executor, change a release profile, or bypass release evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException

from ..config import config
from ..models import DeploymentItem, ReleaseTag
from ..routers.quality import get_quality_report
from . import (
    catalog,
    cloud_build,
    deployment_executions,
    deployment_store,
    executor_circuits,
    github_actions_quota,
    github_deployments,
    github_release_control,
    release_authorization_store,
    release_executions,
)

_GITHUB_UNAVAILABLE = "GitHub unavailable"


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(value: str) -> float:
    try:
        started = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return 0.0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())


def _open_billing_circuit(service, *, reason: str, evidence: str = "") -> None:
    try:
        if not github_release_control.repository_is_private(service.repository):
            return
    except Exception:
        return
    owner = config.github.billing_owner or service.repository.split("/", 1)[0]
    executor_circuits.open_circuit(
        owner,
        reason=reason,
        repository=service.repository,
        evidence=evidence,
    )
    for repository in {
        item.repository for item in catalog.get_services().services if item.repository
    }:
        try:
            if not github_release_control.repository_is_private(repository):
                continue
            github_release_control.set_repository_execution_mode(
                repository, "cloud_build"
            )
        except Exception:
            # The durable circuit is authoritative. A webhook/scheduled
            # reconciliation can retry the economic repository variable later.
            pass


def _service_or_404(service_name: str):
    service = catalog.get_service(service_name)
    if service is None:
        raise HTTPException(status_code=404, detail=f"Unknown service '{service_name}'")
    if not service.repository:
        raise HTTPException(status_code=409, detail="Service has no GitHub repository")
    if not service.deployment_ready:
        blockers = (
            "; ".join(service.deployment_blockers) or "service is not deployment-ready"
        )
        raise HTTPException(
            status_code=409,
            detail=f"Service '{service.service_name}' is not ready for platform deploy: {blockers}",
        )
    return service


def _require_release_quality(service, sha: str) -> None:
    report = get_quality_report(service.service_name, sha, for_release=True)
    if report.quality_gate_status != "PASSED":
        raise HTTPException(
            status_code=409, detail="Release quality evidence is not PASSED"
        )


def _require_orchestrated_release(service, tag: ReleaseTag) -> None:
    settings = config.release_orchestrator
    if not settings.enabled or service.service_name not in {
        *settings.enabled_services,
        *settings.canary_services,
    }:
        return
    execution = release_executions.find(service.repository, tag.sha, "main_release")
    plan = execution.get("release_plan", {}) if execution else {}
    if (
        not execution
        or execution.get("status") != "released"
        or plan.get("git_tag") != tag.name
        or not execution.get("report_hash")
    ):
        raise HTTPException(
            status_code=409,
            detail="Tag is not backed by a published Engineering Platform release",
        )


def _active_deployment(service_name: str) -> DeploymentItem | None:
    return next(
        (
            item
            for item in deployment_store.list_for_service(service_name, limit=100)
            if item.status not in github_deployments.TERMINAL_STATUSES
        ),
        None,
    )


def _require_matching_idempotency(
    existing: DeploymentItem, service_name: str, tag: str, kind: str
) -> None:
    if (
        existing.service_name != service_name
        or existing.tag != tag
        or existing.kind != kind
    ):
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used for another deployment",
        )


def start_cloud_build(service, item: DeploymentItem, *, reason: str) -> DeploymentItem:
    try:
        execution = deployment_executions.get(item.id)
        if execution and execution.get("provider") == "github_actions":
            release_authorization_store.revoke(
                str(execution.get("authorization_jti", ""))
            )
            deployment_executions.transition_provider(
                item.id,
                from_provider="github_actions",
                to_provider="cloud_build",
                reason=reason,
            )
        submitted = cloud_build.submit(item, service, reason=reason)
        github_deployments.set_managed_status(
            submitted,
            state="queued",
            description="Deployment queued",
            log_url=submitted.logs_url,
        )
        return submitted
    except cloud_build.CloudBuildError as exc:
        item.status = "FAILED"
        item.current_stage = "dispatch"
        item.error = str(exc)
        raise github_deployments.GitHubDispatchError(item) from exc


def _dispatch_deploy(service, tag: ReleaseTag, requested_by: str) -> DeploymentItem:
    if github_actions_quota.should_use_cloud_build(
        service.service_name, service.repository
    ):
        owner = config.github.billing_owner or service.repository.split("/", 1)[0]
        if executor_circuits.is_open(owner):
            _open_billing_circuit(
                service,
                reason="persistent_github_actions_billing_circuit",
                evidence="circuit already open",
            )
        item = github_deployments.start_managed_deployment(
            service=service, tag=tag, requested_by=requested_by
        )
        return start_cloud_build(service, item, reason="github_quota_preflight")
    try:
        return github_deployments.start_deployment(
            service=service, tag=tag, requested_by=requested_by
        )
    except github_deployments.GitHubDispatchError as exc:
        if not github_actions_quota.is_quota_error(exc):
            raise
        _open_billing_circuit(
            service,
            reason="github_actions_billing_rejection",
            evidence=str(getattr(exc, "dispatch_error_detail", ""))[:1000],
        )
        return start_cloud_build(service, exc.item, reason="github_quota_dispatch")


def _dispatch_rollback(
    service, target: DeploymentItem, requested_by: str
) -> DeploymentItem:
    if github_actions_quota.should_use_cloud_build(
        service.service_name, service.repository
    ):
        owner = config.github.billing_owner or service.repository.split("/", 1)[0]
        if executor_circuits.is_open(owner):
            _open_billing_circuit(
                service,
                reason="persistent_github_actions_billing_circuit",
                evidence="circuit already open",
            )
        item = github_deployments.start_managed_deployment(
            service=service,
            tag=ReleaseTag(name=target.tag, sha=target.sha),
            requested_by=requested_by,
            kind="rollback",
            target_revision=target.production_revision,
        )
        return start_cloud_build(service, item, reason="github_quota_preflight")
    try:
        return github_deployments.start_rollback(
            service=service, target=target, requested_by=requested_by
        )
    except github_deployments.GitHubDispatchError as exc:
        if not github_actions_quota.is_quota_error(exc):
            raise
        _open_billing_circuit(
            service,
            reason="github_actions_billing_rejection",
            evidence=str(getattr(exc, "dispatch_error_detail", ""))[:1000],
        )
        return start_cloud_build(service, exc.item, reason="github_quota_dispatch")


def _retry_failed_dispatch(
    service,
    existing: DeploymentItem,
    key: str,
    detail: str,
    target_revision: str = "",
    quality_validator: Callable[[object, str], None] | None = None,
) -> DeploymentItem:
    if existing.kind == "deploy":
        (quality_validator or _require_release_quality)(service, existing.sha)
        _require_orchestrated_release(
            service, ReleaseTag(name=existing.tag, sha=existing.sha)
        )
    try:
        if github_actions_quota.should_use_cloud_build(
            service.service_name, service.repository
        ):
            owner = config.github.billing_owner or service.repository.split("/", 1)[0]
            if executor_circuits.is_open(owner):
                _open_billing_circuit(
                    service,
                    reason="persistent_github_actions_billing_circuit",
                    evidence="circuit already open",
                )
            retried = start_cloud_build(service, existing, reason="retry_preflight")
        else:
            retried = github_deployments.retry_dispatch(
                service=service, item=existing, target_revision=target_revision
            )
    except github_deployments.GitHubDispatchError as exc:
        if github_actions_quota.is_quota_error(exc):
            _open_billing_circuit(
                service,
                reason="github_actions_billing_rejection",
                evidence=str(getattr(exc, "dispatch_error_detail", ""))[:1000],
            )
            retried = start_cloud_build(service, exc.item, reason="retry_billing")
            return deployment_store.save(retried, key)
        deployment_store.save(exc.item, key)
        raise HTTPException(status_code=502, detail=detail) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_GITHUB_UNAVAILABLE) from exc
    return deployment_store.save(retried, key)


def reconcile_stalled_dispatches(*, limit: int = 50) -> dict[str, Any]:
    """Fail over deployments GitHub accepted but never started.

    A workflow dispatch can be accepted with HTTP 204 and still create no run
    when the account cannot allocate a runner for a private repository.  The
    request then stays queued forever and blocks the service, so this sweep
    re-reads GitHub, and once the configured timeout elapses it opens the
    billing circuit and hands the exact same request to the Cloud Build
    executor using the original idempotency key.
    """
    timeout = config.cloud_build.deploy_dispatch_timeout_seconds
    results: list[dict[str, Any]] = []
    for item in deployment_store.list_unfinished(limit=limit):
        execution = deployment_executions.get(item.id)
        if execution and execution.get("provider") == "cloud_build":
            continue
        try:
            refreshed = github_deployments.refresh(item)
        except Exception:
            continue
        key = deployment_store.idempotency_key_for(refreshed.id)
        if (
            refreshed.github_run_id
            or refreshed.status in github_deployments.TERMINAL_STATUSES
        ):
            deployment_store.save(refreshed, key)
            continue
        if _age_seconds(refreshed.created_at) < timeout:
            continue
        service = catalog.get_service(refreshed.service_name)
        if service is None:
            continue
        _open_billing_circuit(
            service,
            reason="github_dispatch_without_run",
            evidence=f"deployment={refreshed.id} waited_seconds={timeout}",
        )
        failed = refreshed.model_copy(
            update={
                "status": "FAILED",
                "current_stage": "dispatch",
                "error": github_deployments.GITHUB_WORKFLOW_DISPATCH_FAILED,
                "updated_at": _iso_now(),
            }
        )
        deployment_store.save(failed, key)
        if not github_actions_quota.should_use_cloud_build(
            service.service_name, service.repository
        ):
            # No fallback is configured for this service: surface it instead of
            # dispatching a second request that GitHub would silently drop.
            results.append(
                {"deployment_id": refreshed.id, "result": "no_fallback_configured"}
            )
            continue
        try:
            retried = _retry_failed_dispatch(
                service,
                failed,
                key,
                github_deployments.GITHUB_WORKFLOW_DISPATCH_FAILED,
            )
        except Exception as exc:
            results.append(
                {
                    "deployment_id": refreshed.id,
                    "result": "retry_failed",
                    "error": str(exc)[:200],
                }
            )
            continue
        results.append(
            {
                "deployment_id": refreshed.id,
                "result": "cloud_build_fallback",
                "status": retried.status,
            }
        )
    return {"reconciled": len(results), "items": results}


def start_deployment(
    *,
    service_name: str,
    tag_name: str,
    requested_by: str,
    idempotency_key: str | None = None,
    quality_validator: Callable[[object, str], None] | None = None,
) -> DeploymentItem:
    """Create one eligible tagged release deployment through the private selector."""
    service = _service_or_404(service_name)
    key = idempotency_key or str(uuid.uuid4())
    existing = deployment_store.find_by_idempotency_key(key)
    if existing is not None:
        _require_matching_idempotency(existing, service_name, tag_name, "deploy")
        if existing.status == "FAILED" and existing.current_stage == "dispatch":
            return _retry_failed_dispatch(
                service,
                existing,
                key,
                github_deployments.GITHUB_WORKFLOW_DISPATCH_FAILED,
                quality_validator=quality_validator,
            )
        return existing
    active = _active_deployment(service_name)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Deployment '{active.tag}' is already active for this service (id: {active.id})",
        )
    try:
        tag = github_deployments.get_tag(service.repository, service_name, tag_name)
        if tag is None:
            raise HTTPException(status_code=404, detail=f"Unknown tag '{tag_name}'")
        if not tag.eligible:
            raise HTTPException(status_code=409, detail=tag.reason)
        (quality_validator or _require_release_quality)(service, tag.sha)
        _require_orchestrated_release(service, tag)
        return deployment_store.save(_dispatch_deploy(service, tag, requested_by), key)
    except HTTPException:
        raise
    except github_deployments.GitHubDispatchError as exc:
        try:
            deployment_store.save(exc.item, key)
        except Exception as store_exc:
            raise HTTPException(
                status_code=502, detail=_GITHUB_UNAVAILABLE
            ) from store_exc
        raise HTTPException(
            status_code=502, detail=github_deployments.GITHUB_WORKFLOW_DISPATCH_FAILED
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_GITHUB_UNAVAILABLE) from exc


def start_rollback(
    *,
    service_name: str,
    target_deployment_id: str,
    requested_by: str,
    idempotency_key: str | None = None,
    quality_validator: Callable[[object, str], None] | None = None,
) -> DeploymentItem:
    """Recover a recorded production revision through the same private selector."""
    service = _service_or_404(service_name)
    target = deployment_store.get(target_deployment_id)
    if target is None or target.service_name != service_name:
        raise HTTPException(
            status_code=404, detail="Unknown deployment to roll back to"
        )
    if target.status != "SUCCEEDED" or not target.production_revision:
        raise HTTPException(
            status_code=409,
            detail="Can only roll back to a previously succeeded production revision",
        )
    key = idempotency_key or str(uuid.uuid4())
    existing = deployment_store.find_by_idempotency_key(key)
    if existing is not None:
        _require_matching_idempotency(existing, service_name, target.tag, "rollback")
        if existing.status == "FAILED" and existing.current_stage == "dispatch":
            return _retry_failed_dispatch(
                service,
                existing,
                key,
                github_deployments.GITHUB_ROLLBACK_WORKFLOW_DISPATCH_FAILED,
                target.production_revision,
                quality_validator,
            )
        return existing
    active = _active_deployment(service_name)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Deployment '{active.tag}' is already active for this service (id: {active.id})",
        )
    try:
        return deployment_store.save(
            _dispatch_rollback(service, target, requested_by), key
        )
    except github_deployments.GitHubDispatchError as exc:
        try:
            deployment_store.save(exc.item, key)
        except Exception as store_exc:
            raise HTTPException(
                status_code=502, detail=_GITHUB_UNAVAILABLE
            ) from store_exc
        raise HTTPException(
            status_code=502,
            detail=github_deployments.GITHUB_ROLLBACK_WORKFLOW_DISPATCH_FAILED,
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_GITHUB_UNAVAILABLE) from exc
