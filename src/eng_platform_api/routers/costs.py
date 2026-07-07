"""Costs router — BigQuery billing data."""

from fastapi import APIRouter, Query

from ..models import CostSummary
from ..services import gcp_billing_bigquery as billing

router = APIRouter(prefix="/api/costs", tags=["costs"])


@router.get("/summary", response_model=CostSummary)
async def get_cost_summary(days: int = Query(default=30, ge=1, le=365)):
    """Get cost summary for the specified time window."""
    return billing.get_cost_summary(days=days)


@router.get("/by-service", response_model=CostSummary)
async def get_cost_by_service(days: int = Query(default=30, ge=1, le=365)):
    """Get costs grouped by service."""
    return billing.get_cost_by_service(days=days)


@router.get("/by-app", response_model=CostSummary)
async def get_cost_by_app(days: int = Query(default=30, ge=1, le=365)):
    """Get costs grouped by application label."""
    return billing.get_cost_by_app(days=days)
