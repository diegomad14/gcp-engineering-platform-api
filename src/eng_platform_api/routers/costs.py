"""Costs router — BigQuery billing data."""

from datetime import datetime, timezone
from threading import Lock
from time import monotonic
from typing import Callable, TypeVar

from fastapi import APIRouter, Query

from ..config import config
from ..models import CloudBuildUsage, CostSummary, DailyCostSeries
from ..services import release_executions
from ..services import gcp_billing_bigquery as billing

router = APIRouter(prefix="/api/costs", tags=["costs"])
_CACHE_TTL_SECONDS = 300
_cache: dict[tuple[object, ...], tuple[float, object]] = {}
_cache_lock = Lock()
_T = TypeVar("_T")


def _cached(key: tuple[object, ...], loader: Callable[[], _T]) -> _T:
    if config.mock_mode:
        return loader()
    with _cache_lock:
        cached = _cache.get(key)
        now = monotonic()
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]  # type: ignore[return-value]
        value = loader()
        _cache[key] = (monotonic(), value)
        return value


@router.get("/status")
def get_billing_status():
    """Check if Cloud Billing Export is active."""
    return _cached(("status",), billing.get_billing_status)


@router.get("/summary", response_model=CostSummary)
def get_cost_summary(
    days: int = Query(default=30, ge=1, le=365),
    month_to_date: bool = Query(
        default=False,
        description="Current calendar month (matches the GCP console); ignores `days`.",
    ),
):
    """Get cost summary for the specified time window."""
    summary = _cached(
        ("summary", days, month_to_date),
        lambda: billing.get_cost_summary(days=days, month_to_date=month_to_date),
    )
    return summary.model_copy(update={"cloud_build": _cloud_build_usage()})


def _cloud_build_usage() -> CloudBuildUsage:
    """Attach the exact Cloud Build usage the platform already meters."""
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    try:
        usage = release_executions.monthly_cloud_build_usage(month)
    except Exception:
        return CloudBuildUsage(month=month)
    return CloudBuildUsage.model_validate(usage)


@router.get("/by-service", response_model=CostSummary)
def get_cost_by_service(
    days: int = Query(default=30, ge=1, le=365),
    month_to_date: bool = Query(default=False),
):
    """Get costs grouped by GCP service."""
    return _cached(
        ("service", days, month_to_date),
        lambda: billing.get_cost_by_service(days=days, month_to_date=month_to_date),
    )


@router.get("/by-sku", response_model=CostSummary)
def get_cost_by_sku(
    days: int = Query(default=30, ge=1, le=365),
    month_to_date: bool = Query(default=False),
):
    """Get costs grouped by SKU (SKU description in `service_name`)."""
    return _cached(
        ("sku", days, month_to_date),
        lambda: billing.get_cost_by_sku(days=days, month_to_date=month_to_date),
    )


@router.get("/daily", response_model=DailyCostSeries)
def get_daily_costs(
    days: int = Query(default=30, ge=1, le=365),
    month_to_date: bool = Query(
        default=False,
        description="Current calendar month (previous period = previous month); ignores `days`.",
    ),
):
    """Daily net cost series plus the previous window's total for comparison."""
    return _cached(
        ("daily", days, month_to_date),
        lambda: billing.get_daily_costs(days=days, month_to_date=month_to_date),
    )
