"""Costs router — BigQuery billing data."""

from typing import Literal
from concurrent.futures import Future
from threading import Lock
from time import monotonic
from typing import Callable, TypeVar, cast

from fastapi import APIRouter, Query

from ..config import catalog_source_identity, config
from ..models import CloudBuildUsage, CostComparison, CostSummary, DailyCostSeries
from ..services import cloud_build_usage
from ..services import gcp_billing_bigquery as billing

router = APIRouter(prefix="/api/costs", tags=["costs"])
_CACHE_TTL_SECONDS = 300
_cache: dict[tuple[object, ...], tuple[float, object]] = {}
_inflight: dict[tuple[object, ...], Future[object]] = {}
_cache_lock = Lock()
_T = TypeVar("_T")


def _cached(key: tuple[object, ...], loader: Callable[[], _T]) -> _T:
    if config.mock_mode:
        return loader()
    # A five-minute cache must not carry yesterday across local midnight.
    key = (
        *key,
        catalog_source_identity(),
        billing.utc_now().astimezone(billing._TIMEZONE).date().isoformat(),
    )
    with _cache_lock:
        cached = _cache.get(key)
        now = monotonic()
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cast(_T, cached[1])
        pending = _inflight.get(key)
        owner = pending is None
        if pending is None:
            pending = Future()
            _inflight[key] = pending
    # Only callers for this exact window/key share a load. A slow daily query
    # must not hold up cached responses or unrelated billing summaries.
    if not owner:
        return cast(_T, pending.result())
    try:
        value = loader()
    except BaseException as exc:
        with _cache_lock:
            _inflight.pop(key, None)
        pending.set_exception(exc)
        raise
    with _cache_lock:
        _cache[key] = (monotonic(), value)
        _inflight.pop(key, None)
    pending.set_result(value)
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
        description="Consumption month in America/Bogota; ignores `days`.",
    ),
):
    """Get cost summary for the specified time window."""
    summary = _cached(
        ("summary", days, month_to_date),
        lambda: billing.get_cost_summary(days=days, month_to_date=month_to_date),
    )
    return summary.model_copy(update={"cloud_build": _cloud_build_usage()})


def _cloud_build_usage() -> CloudBuildUsage | None:
    """Read the collector snapshot; never make provider calls from a UI poll."""
    if not config.cloud_build.usage_enabled:
        return None
    # The collector owns a UTC monthly ledger; estimates stay separate from
    # Bogota consumption billing and must never be added to exported totals.
    month = billing.utc_now().strftime("%Y-%m")
    usage = cloud_build_usage.summary(month)
    return CloudBuildUsage.model_validate(usage) if usage else None


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
        description="Bogota month to date; previous period uses equivalent elapsed time.",
    ),
):
    """Daily net cost series plus the previous window's total for comparison."""
    return _cached(
        ("daily", days, month_to_date),
        lambda: billing.get_daily_costs(days=days, month_to_date=month_to_date),
    )


@router.get("/comparison", response_model=CostComparison)
def get_cost_comparison(
    days: int = Query(default=1, ge=1, le=365),
    month_to_date: bool = Query(default=False),
    group_by: Literal["resource", "service", "sku"] = "resource",
):
    """Equivalent Bogota consumption windows, limited by observed usage freshness."""
    return _cached(
        ("comparison", days, month_to_date, group_by),
        lambda: billing.get_cost_comparison(days, month_to_date, group_by),
    )
