"""Service-oriented GitHub deployment endpoints."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, status

from ..models import (
    DeploymentCreateRequest,
    DeploymentItem,
    DeploymentList,
    DeploymentOverview,
    DeploymentOverviewItem,
    ReleaseTag,
    ReleaseTagPage,
)
from ..security import require_deployer
from ..services import (
    catalog,
    cloud_build,
    deployment_executions,
    deployment_store,
    github_actions_quota,
    github_deployments,
    release_authorization_store,
)
from ..services import deployment_commands
from .quality import get_quality_report

router = APIRouter(prefix="/api", tags=["deployments"])
_GITHUB_UNAVAILABLE = "GitHub unavailable"
_OVERVIEW_CACHE_TTL_SECONDS = 30
_overview_cache: tuple[float, DeploymentOverview] | None = None
_overview_cache_lock = Lock()


def _invalidate_overview_cache() -> None:
    global _overview_cache
    with _overview_cache_lock:
        _overview_cache = None


def _service_or_404(service_name: str):
    service = catalog.get_service(service_name)
    if service is None:
        raise HTTPException(status_code=404, detail=f"Unknown service '{service_name}'")
    if not service.repository:
        raise HTTPException(status_code=409, detail="Service has no GitHub repository")
    return service


def _require_release_quality(service, sha: str) -> None:
    report = get_quality_report(service.service_name, sha, for_release=True)
    if report.quality_gate_status != "PASSED":
        raise HTTPException(
            status_code=409, detail="Release quality evidence is not PASSED"
        )


# Compatibility helpers retained for existing internal callers. REST and MCP
# mutations use deployment_commands directly; these functions do not form an
# alternate request surface.
def _active_deployment(service_name: str) -> DeploymentItem | None:
    return next(
        (
            item
            for item in deployment_store.list_for_service(service_name, limit=100)
            if item.status not in github_deployments.TERMINAL_STATUSES
        ),
        None,
    )


def _start_cloud_build(service, item: DeploymentItem, *, reason: str) -> DeploymentItem:
    # Kept as a module attribute for compatibility with the operational tests
    # that patch the release-authorization store used by the shared command.
    assert release_authorization_store is not None
    return deployment_commands.start_cloud_build(service, item, reason=reason)


def _dispatch_deploy(service, tag, requested_by: str) -> DeploymentItem:
    if github_actions_quota.should_use_cloud_build(
        service.service_name, service.repository
    ):
        item = github_deployments.start_managed_deployment(
            service=service, tag=tag, requested_by=requested_by
        )
        return _start_cloud_build(service, item, reason="github_quota_preflight")
    try:
        return github_deployments.start_deployment(
            service=service, tag=tag, requested_by=requested_by
        )
    except github_deployments.GitHubDispatchError as exc:
        if not github_actions_quota.is_quota_error(exc):
            raise
        return _start_cloud_build(service, exc.item, reason="github_quota_dispatch")


def _dispatch_rollback(
    service, target: DeploymentItem, requested_by: str
) -> DeploymentItem:
    if github_actions_quota.should_use_cloud_build(
        service.service_name, service.repository
    ):
        item = github_deployments.start_managed_deployment(
            service=service,
            tag=ReleaseTag(name=target.tag, sha=target.sha),
            requested_by=requested_by,
            kind="rollback",
            target_revision=target.production_revision,
        )
        return _start_cloud_build(service, item, reason="github_quota_preflight")
    try:
        return github_deployments.start_rollback(
            service=service, target=target, requested_by=requested_by
        )
    except github_deployments.GitHubDispatchError as exc:
        if not github_actions_quota.is_quota_error(exc):
            raise
        return _start_cloud_build(service, exc.item, reason="github_quota_dispatch")


def _retry_failed_dispatch(
    service, existing: DeploymentItem, key: str, detail: str, target_revision: str = ""
) -> DeploymentItem:
    return deployment_commands._retry_failed_dispatch(
        service,
        existing,
        key,
        detail,
        target_revision,
        quality_validator=_require_release_quality,
    )


@router.get(
    "/services/{service_name}/tags",
    response_model=ReleaseTagPage,
    responses={
        400: {"description": "Invalid tag cursor"},
        502: {"description": "GitHub or deployment store unavailable"},
    },
)
def list_service_tags(
    service_name: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=100),
):
    service = _service_or_404(service_name)
    try:
        offset = int(cursor or "0")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid tag cursor") from exc
    if offset < 0:
        raise HTTPException(status_code=400, detail="Invalid tag cursor")
    try:
        return github_deployments.list_tags(
            service.repository, service_name, cursor=cursor, limit=limit
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_GITHUB_UNAVAILABLE) from exc


@router.post(
    "/services/{service_name}/deployments",
    response_model=DeploymentItem,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        401: {"description": "GitHub authentication required"},
        403: {"description": "Operator is not allowlisted"},
        404: {"description": "Service or tag not found"},
        409: {"description": "Tag, idempotency, or active deployment conflict"},
        502: {"description": "GitHub is unavailable"},
    },
)
def create_deployment(
    service_name: str,
    payload: DeploymentCreateRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    requested_by = require_deployer(request)
    item = deployment_commands.start_deployment(
        service_name=service_name,
        tag_name=payload.tag,
        requested_by=requested_by,
        idempotency_key=idempotency_key,
        quality_validator=_require_release_quality,
    )
    _invalidate_overview_cache()
    return item


@router.post(
    "/services/{service_name}/deployments/{target_deployment_id}/rollback",
    response_model=DeploymentItem,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        401: {"description": "GitHub authentication required"},
        403: {"description": "Operator is not allowlisted"},
        404: {"description": "Service or target deployment not found"},
        409: {
            "description": "Target is not a succeeded production deployment, or an active deployment already exists"
        },
        502: {"description": "GitHub is unavailable"},
    },
)
def rollback_deployment(
    service_name: str,
    target_deployment_id: str,
    request: Request,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    requested_by = require_deployer(request)
    item = deployment_commands.start_rollback(
        service_name=service_name,
        target_deployment_id=target_deployment_id,
        requested_by=requested_by,
        idempotency_key=idempotency_key,
        quality_validator=_require_release_quality,
    )
    _invalidate_overview_cache()
    return item


@router.get(
    "/services/{service_name}/deployments",
    response_model=DeploymentList,
)
def list_service_deployments(
    service_name: str,
    limit: int = Query(default=20, ge=1, le=100),
):
    _service_or_404(service_name)
    items, total = deployment_store.list_for_service_with_total(
        service_name, limit=limit
    )
    refreshed: list[DeploymentItem] = []
    for item in items:
        if item.status not in github_deployments.TERMINAL_STATUSES:
            try:
                previous = item.model_dump()
                item = _refresh(item)
                if item.model_dump() != previous:
                    deployment_store.save(item, "")
                    _invalidate_overview_cache()
            except Exception:
                item.error = _GITHUB_UNAVAILABLE
        refreshed.append(item)
    return DeploymentList(items=refreshed, total=total)


def _overview_item(
    service, last_deployment: DeploymentItem | None
) -> DeploymentOverviewItem:
    detail = catalog.get_service_detail(service.service_name)
    return DeploymentOverviewItem(
        service_name=service.service_name,
        status=detail.status if detail else "degraded",
        latest_ready_revision=detail.latest_ready_revision if detail else "",
        deployment_ready=service.deployment_ready,
        deployment_blockers=service.deployment_blockers,
        last_deployment=last_deployment,
    )


@router.get("/deployments/overview", response_model=DeploymentOverview)
def get_deployments_overview():
    """Return the deployment list page data with bounded external concurrency."""
    global _overview_cache
    with _overview_cache_lock:
        now = monotonic()
        if _overview_cache and now - _overview_cache[0] < _OVERVIEW_CACHE_TTL_SECONDS:
            return _overview_cache[1]

        services = catalog.get_services().services
        latest = deployment_store.latest_for_services(
            [service.service_name for service in services]
        )
        workers = min(6, max(1, len(services)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            items = list(
                executor.map(
                    lambda service: _overview_item(
                        service, latest.get(service.service_name)
                    ),
                    services,
                )
            )
        result = DeploymentOverview(
            items=items,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
        _overview_cache = (monotonic(), result)
        return result


@router.get("/deployments/{deployment_id}", response_model=DeploymentItem)
def get_deployment(deployment_id: str):
    item = deployment_store.get(deployment_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Deployment not found")
    if item.status not in github_deployments.TERMINAL_STATUSES:
        try:
            previous = item.model_dump()
            item = _refresh(item)
            if item.model_dump() != previous:
                deployment_store.save(item, "")
                _invalidate_overview_cache()
        except Exception:
            item.error = _GITHUB_UNAVAILABLE
    return item


def _refresh(item: DeploymentItem) -> DeploymentItem:
    execution = deployment_executions.get(item.id)
    if execution and execution.get("provider") == "cloud_build":
        refreshed = cloud_build.refresh(item)
        if refreshed.status in github_deployments.TERMINAL_STATUSES:
            github_deployments.set_managed_status(
                refreshed,
                state="success"
                if refreshed.status in {"SUCCEEDED", "ROLLED_BACK"}
                else "failure",
                description=(
                    refreshed.error or f"Deployment {refreshed.status.lower()}"
                ),
                log_url=refreshed.logs_url,
                environment_url=refreshed.production_url,
            )
        return refreshed
    item = github_deployments.refresh(item)
    if item.status != "FAILED" or not item.github_run_id:
        return item
    try:
        run = (
            github_deployments.github_client()
            .get_repo(item.repository)
            .get_workflow_run(item.github_run_id)
        )
        if github_actions_quota.is_reactive_quota_failure(run, item):
            service = _service_or_404(item.service_name)
            return _start_cloud_build(service, item, reason="github_quota_startup")
    except Exception:
        pass
    return item
