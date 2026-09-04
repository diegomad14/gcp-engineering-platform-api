"""Service-oriented GitHub deployment endpoints."""

from __future__ import annotations

import uuid

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
    ReleaseTagPage,
)
from ..security import require_deployer
from ..services import catalog, deployment_store, github_deployments
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


def _require_deployment_ready(service) -> None:
    if service.deployment_ready:
        return
    blockers = (
        "; ".join(service.deployment_blockers) or "service is not deployment-ready"
    )
    raise HTTPException(
        status_code=409,
        detail=f"Service '{service.service_name}' is not ready for platform deploy: {blockers}",
    )


def _require_release_quality(service, sha: str) -> None:
    report = get_quality_report(service.service_name, sha, for_release=True)
    if report.quality_gate_status != "PASSED":
        raise HTTPException(
            status_code=409, detail="Release quality evidence is not PASSED"
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
    existing: DeploymentItem,
    service_name: str,
    tag: str,
    kind: str,
    runner_label: str = "",
    contingency_cause: str = "",
) -> None:
    if (
        existing.service_name != service_name
        or existing.tag != tag
        or existing.kind != kind
        or existing.runner_label != runner_label
        or existing.contingency_cause != contingency_cause
    ):
        raise HTTPException(
            status_code=409,
            detail="Idempotency-Key was already used for another deployment",
        )


def _save_dispatch_error(
    exc: github_deployments.GitHubDispatchError, key: str, detail: str
) -> None:
    try:
        deployment_store.save(exc.item, key)
        _invalidate_overview_cache()
    except Exception as store_exc:
        raise HTTPException(status_code=502, detail=_GITHUB_UNAVAILABLE) from store_exc
    raise HTTPException(status_code=502, detail=detail) from exc


def _retry_failed_dispatch(
    service, existing: DeploymentItem, key: str, detail: str, target_revision: str = ""
) -> DeploymentItem:
    if existing.kind == "deploy":
        _require_release_quality(service, existing.sha)
    try:
        retried = github_deployments.retry_dispatch(
            service=service, item=existing, target_revision=target_revision
        )
    except github_deployments.GitHubDispatchError as exc:
        deployment_store.save(exc.item, key)
        _invalidate_overview_cache()
        raise HTTPException(status_code=502, detail=detail) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_GITHUB_UNAVAILABLE) from exc
    saved = deployment_store.save(retried, key)
    _invalidate_overview_cache()
    return saved


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
    service = _service_or_404(service_name)
    _require_deployment_ready(service)
    requested_by = require_deployer(request)
    key = idempotency_key or str(uuid.uuid4())
    existing = deployment_store.find_by_idempotency_key(key)
    if existing is not None:
        _require_matching_idempotency(
            existing,
            service_name,
            payload.tag,
            "deploy",
            payload.runner_label,
            payload.contingency_cause,
        )
        if existing.status == "FAILED" and existing.current_stage == "dispatch":
            return _retry_failed_dispatch(
                service,
                existing,
                key,
                github_deployments.GITHUB_WORKFLOW_DISPATCH_FAILED,
            )
        return existing
    active = _active_deployment(service_name)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Deployment '{active.tag}' is already active for this service "
                f"(id: {active.id})"
            ),
        )
    try:
        tag = github_deployments.get_tag(service.repository, service_name, payload.tag)
        if tag is None:
            raise HTTPException(status_code=404, detail=f"Unknown tag '{payload.tag}'")
        if not tag.eligible:
            raise HTTPException(status_code=409, detail=tag.reason)
        _require_release_quality(service, tag.sha)
        item = github_deployments.start_deployment(
            service=service,
            tag=tag,
            requested_by=requested_by,
            runner_label=payload.runner_label,
            contingency_cause=payload.contingency_cause,
        )
        saved = deployment_store.save(item, key)
        _invalidate_overview_cache()
        return saved
    except HTTPException:
        raise
    except github_deployments.GitHubDispatchError as exc:
        _save_dispatch_error(
            exc, key, github_deployments.GITHUB_WORKFLOW_DISPATCH_FAILED
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_GITHUB_UNAVAILABLE) from exc


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
    service = _service_or_404(service_name)
    _require_deployment_ready(service)
    requested_by = require_deployer(request)
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
            )
        return existing
    active = _active_deployment(service_name)
    if active is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Deployment '{active.tag}' is already active for this service "
                f"(id: {active.id})"
            ),
        )
    try:
        item = github_deployments.start_rollback(
            service=service,
            target=target,
            requested_by=requested_by,
        )
        saved = deployment_store.save(item, key)
        _invalidate_overview_cache()
        return saved
    except HTTPException:
        raise
    except github_deployments.GitHubDispatchError as exc:
        _save_dispatch_error(
            exc, key, github_deployments.GITHUB_ROLLBACK_WORKFLOW_DISPATCH_FAILED
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_GITHUB_UNAVAILABLE) from exc


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
                item = github_deployments.refresh(item)
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
            item = github_deployments.refresh(item)
            if item.model_dump() != previous:
                deployment_store.save(item, "")
                _invalidate_overview_cache()
        except Exception:
            item.error = _GITHUB_UNAVAILABLE
    return item
