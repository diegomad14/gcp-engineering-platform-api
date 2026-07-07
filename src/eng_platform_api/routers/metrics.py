"""Metrics router — Cloud Run operational metrics."""

from fastapi import APIRouter

from ..models import MetricsSummary
from ..services import gcp_monitoring

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("/cloud-run/summary", response_model=MetricsSummary)
async def get_cloud_run_metrics():
    """Get Cloud Run metrics summary for all tracked services."""
    return gcp_monitoring.get_metrics_summary()
