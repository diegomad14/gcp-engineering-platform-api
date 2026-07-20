"""Catalog router — independent service metadata."""

import anyio
from fastapi import APIRouter, HTTPException

from ..models import CatalogResponse, ServiceDetail
from ..services import catalog as catalog_service

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/services", response_model=CatalogResponse)
async def list_services():
    """List all registered services."""
    return catalog_service.get_services()


@router.get("/services/{service_name}", response_model=ServiceDetail)
async def get_service(service_name: str):
    """Get service metadata and best-effort live Cloud Run state."""
    service = await anyio.to_thread.run_sync(
        catalog_service.get_service_detail, service_name
    )
    if service is None:
        raise HTTPException(
            status_code=404, detail=f"Service '{service_name}' not found"
        )
    return service
