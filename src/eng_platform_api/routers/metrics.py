"""Metrics router — Cloud Run operational metrics."""

import anyio
from fastapi import APIRouter, Query
from typing import Literal

from ..models import MetricsSummary
from ..services import gcp_monitoring

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


@router.get("/cloud-run/summary", response_model=MetricsSummary)
async def get_cloud_run_metrics(
    window: Literal["1h", "24h"] = Query(default="24h"),
):
    """Get Cloud Run metrics summary for all tracked services."""
    minutes = 60 if window == "1h" else 1440
    return await anyio.to_thread.run_sync(gcp_monitoring.get_metrics_summary, minutes)
