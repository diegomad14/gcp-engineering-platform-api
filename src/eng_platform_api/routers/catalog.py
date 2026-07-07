"""Catalog router — application and service metadata."""

from fastapi import APIRouter, HTTPException

from ..models import Application, CatalogResponse
from ..services import catalog as catalog_service

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


@router.get("/apps", response_model=CatalogResponse)
async def list_applications():
    """List all registered applications."""
    return catalog_service.get_applications()


@router.get("/apps/{app_id}", response_model=Application)
async def get_application(app_id: str):
    """Get a single application by ID."""
    app = catalog_service.get_application(app_id)
    if app is None:
        raise HTTPException(status_code=404, detail=f"Application '{app_id}' not found")
    return app
