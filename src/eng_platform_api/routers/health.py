"""Health check router."""

from concurrent.futures import ThreadPoolExecutor
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
def services_health_check():
    """Aggregate best-effort health for catalog Cloud Run services."""
    checked_at = datetime.now(timezone.utc).isoformat()
    catalog_services = catalog.get_services().services

    def inspect(service) -> ServiceHealthItem:
        detail = catalog.get_service_detail(service.service_name)
        return ServiceHealthItem(
            service_name=service.service_name,
            project_id=service.project_id,
            region=service.region,
            status=detail.status if detail else "degraded",
            checked_at=checked_at,
            error=detail.error if detail else "Service not found",
        )

    workers = min(6, max(1, len(catalog_services)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        services = list(executor.map(inspect, catalog_services))

    aggregate_status = "ok"
    if any(service.status == "degraded" for service in services):
        aggregate_status = "degraded"

    return ServicesHealthResponse(status=aggregate_status, services=services)
