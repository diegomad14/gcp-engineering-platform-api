"""Health check router."""

from datetime import datetime, timezone

from fastapi import APIRouter
from ..config import config
from ..models import HealthResponse, ServiceHealthItem, ServicesHealthResponse
from ..services import catalog

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Platform API health check."""
    return HealthResponse(
        status="ok",
        version="0.4.1",
        mock_mode=config.mock_mode,
    )


@router.get("/api/health/services", response_model=ServicesHealthResponse)
async def services_health_check():
    """Aggregate best-effort health for catalog Cloud Run services."""
    checked_at = datetime.now(timezone.utc).isoformat()
    services: list[ServiceHealthItem] = []
    for service in catalog.get_services().services:
        detail = catalog.get_service_detail(service.service_name)
        services.append(
            ServiceHealthItem(
                service_name=service.service_name,
                project_id=service.project_id,
                region=service.region,
                status=detail.status if detail else "degraded",
                checked_at=checked_at,
                error=detail.error if detail else "Service not found",
            )
        )

    aggregate_status = "ok"
    if any(service.status == "degraded" for service in services):
        aggregate_status = "degraded"

    return ServicesHealthResponse(status=aggregate_status, services=services)
