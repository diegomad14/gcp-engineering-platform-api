"""Catalog router — independent service metadata."""

from fastapi import APIRouter, HTTPException, Request

from ..models import CatalogResponse, ServiceDetail
from ..services import catalog as catalog_service
from ..services.log_catalog import CatalogUnavailable

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/services", response_model=CatalogResponse)
def list_services(request: Request):
    """List all registered services."""
    try:
        return catalog_service.get_services(
            getattr(request.state, "catalog_visible_services", None)
        )
    except CatalogUnavailable:
        raise HTTPException(
            status_code=503, detail="Runtime catalog is unavailable"
        ) from None


@router.get("/services/{service_name}", response_model=ServiceDetail)
def get_service(service_name: str):
    """Get service metadata and best-effort live Cloud Run state."""
    try:
        service = catalog_service.get_service_detail(service_name)
    except CatalogUnavailable:
        raise HTTPException(
            status_code=503, detail="Runtime catalog is unavailable"
        ) from None
    if service is None:
        raise HTTPException(
            status_code=404, detail=f"Service '{service_name}' not found"
        )
    return service
