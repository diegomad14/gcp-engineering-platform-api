"""Service-oriented GitHub deployment endpoints."""

from __future__ import annotations

import uuid

from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, status

from ..models import (
    DeploymentCreateRequest,
    DeploymentItem,
    DeploymentList,
    ReleaseTagPage,
)
from ..security import require_deployer
from ..services import catalog, deployment_store, github_deployments

router = APIRouter(prefix="/api", tags=["deployments"])
_GITHUB_UNAVAILABLE = "GitHub unavailable"


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


def _active_deployment(service_name: str) -> DeploymentItem | None:
    return next(
        (
            item
            for item in deployment_store.list_for_service(service_name, limit=100)
            if item.status not in github_deployments.TERMINAL_STATUSES
        ),
        None,
    )


@router.get(
    "/services/{service_name}/tags",
    response_model=ReleaseTagPage,
    responses={
        400: {"description": "Invalid tag cursor"},
        502: {"description": "GitHub or deployment store unavailable"},
    },
)
async def list_service_tags(
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
async def create_deployment(
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
        if existing.service_name != service_name or existing.tag != payload.tag:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used for another deployment",
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
        item = github_deployments.start_deployment(
            service=service,
            tag=tag,
            requested_by=requested_by,
        )
        return deployment_store.save(item, key)
    except HTTPException:
        raise
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
async def rollback_deployment(
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
        if existing.service_name != service_name or existing.tag != target.tag:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used for another deployment",
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
        return deployment_store.save(item, key)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_GITHUB_UNAVAILABLE) from exc


@router.get(
    "/services/{service_name}/deployments",
    response_model=DeploymentList,
)
async def list_service_deployments(
    service_name: str,
    limit: int = Query(default=20, ge=1, le=100),
):
    _service_or_404(service_name)
    items = deployment_store.list_for_service(service_name, limit=limit)
    refreshed: list[DeploymentItem] = []
    for item in items:
        try:
            item = github_deployments.refresh(item)
        except Exception:
            item.error = _GITHUB_UNAVAILABLE
        deployment_store.save(item, "")
        refreshed.append(item)
    return DeploymentList(
        items=refreshed,
        total=deployment_store.count_for_service(service_name),
    )


@router.get("/deployments/{deployment_id}", response_model=DeploymentItem)
async def get_deployment(deployment_id: str):
    item = deployment_store.get(deployment_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Deployment not found")
    try:
        item = github_deployments.refresh(item)
        deployment_store.save(item, "")
    except Exception:
        item.error = _GITHUB_UNAVAILABLE
    return item
