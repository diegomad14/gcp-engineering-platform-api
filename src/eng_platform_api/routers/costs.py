"""Costs router — BigQuery billing data."""

from fastapi import APIRouter, Query

from ..models import CostSummary, DailyCostSeries
from ..services import gcp_billing_bigquery as billing

router = APIRouter(prefix="/api/costs", tags=["costs"])


@router.get("/status")
async def get_billing_status():
    """Check if Cloud Billing Export is active."""
    return billing.get_billing_status()


@router.get("/summary", response_model=CostSummary)
async def get_cost_summary(
    days: int = Query(default=30, ge=1, le=365),
    month_to_date: bool = Query(
        default=False,
        description="Current calendar month (matches the GCP console); ignores `days`.",
    ),
):
    """Get cost summary for the specified time window."""
    return billing.get_cost_summary(days=days, month_to_date=month_to_date)


@router.get("/by-service", response_model=CostSummary)
async def get_cost_by_service(
    days: int = Query(default=30, ge=1, le=365),
    month_to_date: bool = Query(default=False),
):
    """Get costs grouped by GCP service."""
    return billing.get_cost_by_service(days=days, month_to_date=month_to_date)


@router.get("/by-sku", response_model=CostSummary)
async def get_cost_by_sku(
    days: int = Query(default=30, ge=1, le=365),
    month_to_date: bool = Query(default=False),
):
    """Get costs grouped by SKU (SKU description in `service_name`)."""
    return billing.get_cost_by_sku(days=days, month_to_date=month_to_date)


@router.get("/daily", response_model=DailyCostSeries)
async def get_daily_costs(
    days: int = Query(default=30, ge=1, le=365),
    month_to_date: bool = Query(
        default=False,
        description="Current calendar month (previous period = previous month); ignores `days`.",
    ),
):
    """Daily net cost series plus the previous window's total for comparison."""
    return billing.get_daily_costs(days=days, month_to_date=month_to_date)
