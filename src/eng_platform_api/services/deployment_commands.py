"""Shared, provider-neutral deployment commands.

Both REST and MCP call this module.  Keeping the command boundary here is
important: callers can authenticate differently, but cannot select an
executor, change a release profile, or bypass release evidence.
"""

from __future__ import annotations

from datetime import datetime, timezone
import uuid
from collections.abc import Callable
from functools import wraps
from typing import Any

from fastapi import HTTPException

from ..config import config
from ..models import DeploymentItem, ReleaseTag, ExecutionProvider
from ..routers.quality import get_quality_report
from . import (
    catalog,
    provider_selection,
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

from .resource_access import require_managed, require_managed_service_name
from .cloud_build_policy import require_new_builds

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
    if not config.cloud_build.new_builds_enabled:
        return
    if config.release_orchestrator.private_executor_mode in {
        "cloud_build",
        "github_actions",
    }:
        # A late real rejection must not suppress another reserved GitHub job.
        return
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


def _service_or_404(service_name: str, provider: ExecutionProvider | None = None):
    require_managed_service_name(service_name)
    service = catalog.get_service(service_name)
    if service is None:
        raise HTTPException(status_code=404, detail=f"Unknown service '{service_name}'")
    if not service.repository:
        raise HTTPException(status_code=409, detail="Service has no GitHub repository")
    if provider:
        readiness_service = service.model_copy(
            update={
                "deployment": service.deployment.model_copy(
                    update={"executor": provider}
                )
            }
        )
        selected_blockers = catalog.deployment_blockers(readiness_service)
        service = service.model_copy(
            update={
                "deployment_ready": not selected_blockers,
                "deployment_blockers": selected_blockers,
            }
        )
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
            or (deployment_executions.get(item.id) or {}).get("status")
            in {"GITHUB_UNCERTAIN", "GITHUB_DISPATCHING"}
        ),
        None,
    )


def _require_matching_idempotency(
    existing: DeploymentItem,
    service_name: str,
    tag: str,
    kind: str,
    target_deployment_id: str = "",
    target_revision: str = "",
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
    if kind == "rollback" and (
        existing.production_revision != target_revision
        or (
            existing.rollback_target_deployment_id
            and existing.rollback_target_deployment_id != target_deployment_id
        )
    ):
        raise HTTPException(
            409, "Idempotency-Key keeps its original rollback target and revision"
        )


def start_cloud_build(service, item: DeploymentItem, *, reason: str) -> DeploymentItem:
    service = require_managed(service)
    require_managed_service_name(item.service_name)
    require_new_builds()
    try:
        execution = deployment_executions.get(item.id)
        if (
            item.provider_selection
            and item.provider_selection.effective != "cloud_build"
        ):
            raise cloud_build.CloudBuildError(
                "Reserved GitHub operation cannot switch to Cloud Build"
            )
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


def _cloud_build_preflight_reason(service) -> str:
    if github_actions_quota.explicit_private_policy(service.service_name):
        return "explicit_executor_policy"
    owner = config.github.billing_owner or service.repository.split("/", 1)[0]
    if executor_circuits.is_open(owner):
        _open_billing_circuit(
            service,
            reason="persistent_github_actions_billing_circuit",
            evidence="circuit already open",
        )
    return "github_quota_preflight"


def _selection(service, requested: ExecutionProvider | None):
    current = provider_selection.current()
    if current is not None:
        return current
    legacy: ExecutionProvider = "github_actions"
    if requested is None and service.deployment.executor == "auto":
        if github_actions_quota.should_use_cloud_build(
            service.service_name, service.repository
        ):
            legacy = "cloud_build"
    return provider_selection.select(
        service, requested, operation="deploy", legacy_provider=legacy
    )


def _dispatch_deploy(
    service,
    tag: ReleaseTag,
    requested_by: str,
    provider: ExecutionProvider | None = None,
) -> DeploymentItem:
    selection = _selection(service, provider)
    with provider_selection.using(selection):
        if selection.effective == "cloud_build":
            require_new_builds()
            item = github_deployments.start_managed_deployment(
                service=service, tag=tag, requested_by=requested_by
            )
            item.provider_selection = selection
            deployment_store.persist_before_dispatch(item)
            return start_cloud_build(service, item, reason="selected_provider")
        try:
            item = github_deployments.start_deployment(
                service=service, tag=tag, requested_by=requested_by
            )
        except github_deployments.GitHubDispatchError as exc:
            exc.item.provider_selection = selection
            raise
        item.provider_selection = selection
        return item


def _dispatch_rollback(
    service,
    target: DeploymentItem,
    requested_by: str,
    provider: ExecutionProvider | None = None,
) -> DeploymentItem:
    selection = _selection(service, provider)
    with provider_selection.using(selection):
        if selection.effective == "cloud_build":
            require_new_builds()
            item = github_deployments.start_managed_deployment(
                service=service,
                tag=ReleaseTag(name=target.tag, sha=target.sha),
                requested_by=requested_by,
                kind="rollback",
                target_revision=target.production_revision,
            )
            item.provider_selection = selection
            item.production_revision = target.production_revision
            item.rollback_target_deployment_id = target.id
            deployment_store.persist_before_dispatch(item)
            return start_cloud_build(service, item, reason="selected_provider")
        try:
            item = github_deployments.start_rollback(
                service=service, target=target, requested_by=requested_by
            )
        except github_deployments.GitHubDispatchError as exc:
            exc.item.provider_selection = selection
            raise
        item.provider_selection = selection
        item.production_revision = target.production_revision
        item.rollback_target_deployment_id = target.id
        return item


def _retry_failed_dispatch(
    service,
    existing: DeploymentItem,
    key: str,
    detail: str,
    target_revision: str = "",
    quality_validator: Callable[[object, str], None] | None = None,
) -> DeploymentItem:
    service = require_managed(service)
    require_managed_service_name(existing.service_name)
    if existing.kind == "deploy":
        (quality_validator or _require_release_quality)(service, existing.sha)
        _require_orchestrated_release(
            service, ReleaseTag(name=existing.tag, sha=existing.sha)
        )
    try:
        execution = deployment_executions.get(existing.id)
        if execution and execution.get("status") in {
            "GITHUB_UNCERTAIN",
            "GITHUB_DISPATCHING",
            "GITHUB_ACCEPTED",
        }:
            raise HTTPException(
                409,
                "GitHub dispatch is pending or uncertain; reconcile before retrying",
            )
        provider = (
            existing.provider_selection.effective
            if existing.provider_selection
            else execution.get("provider")
            if execution
            else None
        )
        if provider in {"github_actions", "cloud_build"}:
            use_cloud_build = provider == "cloud_build"
        else:
            raise HTTPException(
                status_code=409,
                detail="Existing deployment provider requires reconciliation",
            )
        if use_cloud_build:
            retried = start_cloud_build(service, existing, reason="original_provider")
        else:
            retried = github_deployments.retry_dispatch(
                service=service, item=existing, target_revision=target_revision
            )
    except github_deployments.GitHubDispatchError as exc:
        deployment_store.save(exc.item, key)
        raise HTTPException(status_code=502, detail=detail) from exc
    except executor_circuits.CircuitRecoveryRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_GITHUB_UNAVAILABLE) from exc
    return deployment_store.save(retried, key)


def reconcile_stalled_dispatches(*, limit: int = 50) -> dict[str, Any]:
    """Surface missing GitHub runs without inferring a billing rejection.

    A timeout may be a delivery delay or configuration error. Keep the original
    executor and idempotency key while waiting for a real run or investigation;
    a second executor would risk duplicate deployment and unintended spending.
    """
    timeout = config.cloud_build.deploy_dispatch_timeout_seconds
    results: list[dict[str, Any]] = []
    for item in deployment_store.list_unfinished(limit=limit):
        try:
            require_managed_service_name(item.service_name)
        except HTTPException:
            continue
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
        diagnostic = (
            "GitHub workflow has not appeared before the dispatch timeout; "
            "billing rejection is unconfirmed. Automatic fallback was not started."
        )
        if refreshed.error != diagnostic:
            refreshed = refreshed.model_copy(
                update={"error": diagnostic, "updated_at": _iso_now()}
            )
            deployment_store.save(refreshed, key)
        results.append(
            {"deployment_id": refreshed.id, "result": "awaiting_github_evidence"}
        )
    return {"reconciled": len(results), "items": results}


def _guard_command(command):
    @wraps(command)
    def guarded(*, service_name, **kwargs):
        require_managed_service_name(service_name)
        key = kwargs.get("idempotency_key") or str(uuid.uuid4())
        kwargs["idempotency_key"] = key
        with (
            deployment_store.command_guard("idempotency:" + key),
            deployment_store.command_guard(service_name),
        ):
            return command(service_name=service_name, **kwargs)

    return guarded


@_guard_command
def start_deployment(
    *,
    service_name: str,
    tag_name: str,
    requested_by: str,
    idempotency_key: str | None = None,
    provider: ExecutionProvider | None = None,
    quality_validator: Callable[[object, str], None] | None = None,
) -> DeploymentItem:
    """Create one eligible tagged release deployment through the private selector."""
    key = idempotency_key or str(uuid.uuid4())
    existing = deployment_store.find_by_idempotency_key(key)
    recorded_provider = (
        existing.provider_selection.effective
        if existing and existing.provider_selection
        else (deployment_executions.get(existing.id) or {}).get("provider")
        if existing
        else provider
    )
    service = _service_or_404(service_name, recorded_provider)
    if existing is not None:
        _require_matching_idempotency(existing, service_name, tag_name, "deploy")
        provider_selection.require_same(
            existing.provider_selection,
            provider,
            legacy_provider=(deployment_executions.get(existing.id) or {}).get(
                "provider"
            ),
        )
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
        chosen = _selection(service, provider)
        if chosen.effective == "cloud_build":
            require_new_builds()
        # Persist before even creating the external GitHub Deployment object.
        # A crash/save error leaves this nonterminal admission closed.
        deployment_store.save(
            DeploymentItem(
                id="intent-" + str(uuid.uuid4()),
                service_name=service_name,
                repository=service.repository,
                tag=tag.name,
                sha=tag.sha,
                requested_by=requested_by,
                provider_selection=chosen,
                status="QUEUED",
                current_stage="dispatch_pending",
                created_at=_iso_now(),
                updated_at=_iso_now(),
            ),
            key,
        )
        with deployment_store.using_command_key(key), provider_selection.using(chosen):
            return deployment_store.save(
                _dispatch_deploy(service, tag, requested_by, provider), key
            )
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
    except executor_circuits.CircuitRecoveryRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_GITHUB_UNAVAILABLE) from exc


@_guard_command
def start_rollback(
    *,
    service_name: str,
    target_deployment_id: str,
    requested_by: str,
    idempotency_key: str | None = None,
    provider: ExecutionProvider | None = None,
    quality_validator: Callable[[object, str], None] | None = None,
) -> DeploymentItem:
    """Recover a recorded production revision through the same private selector."""
    key = idempotency_key or str(uuid.uuid4())
    existing = deployment_store.find_by_idempotency_key(key)
    recorded_provider = (
        existing.provider_selection.effective
        if existing and existing.provider_selection
        else (deployment_executions.get(existing.id) or {}).get("provider")
        if existing
        else provider
    )
    service = _service_or_404(service_name, recorded_provider)
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
    if existing is not None:
        _require_matching_idempotency(
            existing,
            service_name,
            target.tag,
            "rollback",
            target.id,
            target.production_revision,
        )
        provider_selection.require_same(
            existing.provider_selection,
            provider,
            legacy_provider=(deployment_executions.get(existing.id) or {}).get(
                "provider"
            ),
        )
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
        chosen = _selection(service, provider)
        if chosen.effective == "cloud_build":
            require_new_builds()
        deployment_store.save(
            DeploymentItem(
                id="intent-" + str(uuid.uuid4()),
                service_name=service_name,
                repository=service.repository,
                tag=target.tag,
                sha=target.sha,
                kind="rollback",
                production_revision=target.production_revision,
                rollback_target_deployment_id=target.id,
                requested_by=requested_by,
                provider_selection=chosen,
                status="QUEUED",
                current_stage="dispatch_pending",
                created_at=_iso_now(),
                updated_at=_iso_now(),
            ),
            key,
        )
        with deployment_store.using_command_key(key), provider_selection.using(chosen):
            return deployment_store.save(
                _dispatch_rollback(service, target, requested_by, provider), key
            )
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
            status_code=502,
            detail=github_deployments.GITHUB_ROLLBACK_WORKFLOW_DISPATCH_FAILED,
        ) from exc
    except executor_circuits.CircuitRecoveryRequired as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_GITHUB_UNAVAILABLE) from exc
