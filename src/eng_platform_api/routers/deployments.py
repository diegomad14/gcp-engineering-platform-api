"""Service-oriented GitHub deployment endpoints."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException, Query, Request, status

from ..models import (
    DeploymentCreateRequest,
    DeploymentItem,
    DeploymentList,
    ReleaseTagPage,
)
from ..security import get_identity
from ..services import catalog, deployment_store, github_deployments

router = APIRouter(prefix="/api", tags=["deployments"])


def _service_or_404(service_name: str):
    service = catalog.get_service(service_name)
    if service is None:
        raise HTTPException(status_code=404, detail=f"Unknown service '{service_name}'")
    if not service.repository:
        raise HTTPException(status_code=409, detail="Service has no GitHub repository")
    return service


@router.get(
    "/services/{service_name}/tags",
    response_model=ReleaseTagPage,
)
async def list_service_tags(
    service_name: str,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=100),
):
    service = _service_or_404(service_name)
    try:
        return github_deployments.list_tags(
            service.repository, service_name, cursor=cursor, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid tag cursor") from exc
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"GitHub unavailable: {exc}"
        ) from exc


@router.post(
    "/services/{service_name}/deployments",
    response_model=DeploymentItem,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_deployment(
    service_name: str,
    payload: DeploymentCreateRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    service = _service_or_404(service_name)
    key = idempotency_key or str(uuid.uuid4())
    existing = deployment_store.find_by_idempotency_key(key)
    if existing is not None:
        return existing
    try:
        tag = github_deployments.get_tag(service.repository, service_name, payload.tag)
        if tag is None:
            raise HTTPException(status_code=404, detail=f"Unknown tag '{payload.tag}'")
        if not tag.eligible:
            raise HTTPException(status_code=409, detail=tag.reason)
        item = github_deployments.start_deployment(
            repository=service.repository,
            service_name=service_name,
            tag=tag,
            requested_by=get_identity(request),
        )
        return deployment_store.save(item, key)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"GitHub unavailable: {exc}"
        ) from exc


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
        except Exception as exc:
            item.error = f"GitHub unavailable: {exc}"
        deployment_store.save(item, "")
        refreshed.append(item)
    return DeploymentList(items=refreshed, total=len(refreshed))


@router.get("/deployments/{deployment_id}", response_model=DeploymentItem)
async def get_deployment(deployment_id: str):
    item = deployment_store.get(deployment_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Deployment not found")
    try:
        item = github_deployments.refresh(item)
        deployment_store.save(item, "")
    except Exception as exc:
        item.error = f"GitHub unavailable: {exc}"
    return item
