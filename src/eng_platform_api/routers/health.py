"""Health check router."""

from datetime import datetime, timezone

from fastapi import APIRouter
from google.cloud import run_v2

from ..config import config
from ..models import HealthResponse, ServiceHealthItem, ServicesHealthResponse
from ..services import catalog

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Platform API health check."""
    return HealthResponse(
        status="ok",
        version="0.1.0",
        mock_mode=config.mock_mode,
    )


@router.get("/api/health/services", response_model=ServicesHealthResponse)
async def services_health_check():
    """Aggregate best-effort health for catalog Cloud Run services."""
    checked_at = datetime.now(timezone.utc).isoformat()
    services: list[ServiceHealthItem] = []
    client = None
    client_error = ""
    if not config.mock_mode:
        try:
            client = run_v2.ServicesClient()
        except Exception as exc:
            client_error = str(exc)

    for app in catalog.get_applications().applications:
        for target in app.release_targets:
            item = ServiceHealthItem(
                app_id=app.id,
                app_name=app.name,
                service_name=target.service_name,
                project_id=target.project_id,
                region=target.region,
                status="healthy" if config.mock_mode else "unknown",
                checked_at=checked_at,
            )

            if not config.mock_mode and client is None:
                item.status = "degraded"
                item.error = client_error or "Cloud Run client unavailable"
            elif not config.mock_mode and client is not None:
                try:
                    service = client.get_service(
                        name=(
                            f"projects/{target.project_id}/locations/{target.region}"
                            f"/services/{target.service_name}"
                        )
                    )
                    ready = any(
                        condition.type_ == "Ready" and condition.state.name == "CONDITION_SUCCEEDED"
                        for condition in service.conditions
                    )
                    item.status = "healthy" if ready else "degraded"
                    if not ready:
                        item.error = "Cloud Run service is not Ready"
                except Exception as exc:
                    item.status = "degraded"
                    item.error = str(exc)

            services.append(item)

    aggregate_status = "ok"
    if any(service.status == "degraded" for service in services):
        aggregate_status = "degraded"

    return ServicesHealthResponse(status=aggregate_status, services=services)
