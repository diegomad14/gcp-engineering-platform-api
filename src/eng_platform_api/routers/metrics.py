"""Metrics router — Cloud Run operational metrics."""

import anyio
from fastapi import APIRouter

from ..models import MetricsSummary
from ..services import gcp_monitoring

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("/cloud-run/summary", response_model=MetricsSummary)
async def get_cloud_run_metrics():
    """Get Cloud Run metrics summary for all tracked services."""
    return await anyio.to_thread.run_sync(gcp_monitoring.get_metrics_summary)
