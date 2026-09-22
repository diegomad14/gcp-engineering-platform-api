"""Shared, provider-neutral deployment commands.

Both REST and MCP call this module.  Keeping the command boundary here is
important: callers can authenticate differently, but cannot select an
executor, change a release profile, or bypass release evidence.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

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
