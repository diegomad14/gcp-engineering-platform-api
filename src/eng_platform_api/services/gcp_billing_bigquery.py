"""BigQuery billing service — cost queries with fallback to estimates.

Requires Cloud Billing Export enabled in GCP Console:
https://console.cloud.google.com/billing/01CBB5-464EAA-96C8AC
"""

from datetime import date, timedelta
from typing import Optional

from google.cloud import bigquery

from ..config import config
from ..models import CostItem, CostPeriod, CostSummary

_PROJECT_ID = "cgm-assistant-prod"
_DATASET = "billing_export"


def _billing_table_exists() -> Optional[str]:
    """Check if billing export table exists and return its full ID."""
    if config.mock_mode:
        return None

    try:
        client = bigquery.Client(project=_PROJECT_ID)
        dataset_ref = client.dataset(_DATASET)
        tables = list(client.list_tables(dataset_ref, max_results=10))
        for table in tables:
            table_id = table.table_id
            if table_id.startswith("gcp_billing_export"):
                return f"{_PROJECT_ID}.{_DATASET}.{table_id}"
        return None
    except Exception:
        return None


def _query_billing(table_fqn: str, where_clause: str) -> tuple[list[CostItem], dict]:
    """Execute real BigQuery cost query for the given ``where_clause`` window.

    Returns ``(items, totals)`` where:
      - ``items`` is the line items to display: any group with real money
      movement (cost or credits > $0.01), top 50 by net cost. Fully-credited
        services (e.g. Networking or Cloud Run) stay visible so the list
        reconciles with ``totals``; only genuinely $0.00 groups are dropped.
      - ``totals`` are the period-wide sums over *every* row, so the header
        reconciles with the GCP billing console. The display filter/limit must
        not skew the totals: dropping near-zero-net groups (e.g. Cloud Run fully
        covered by credits) would otherwise understate both cost and credits.

    ``where_clause`` is built by :func:`get_cost_summary` and already includes
    the ``cost_type = 'regular'`` guard.
    """
    items_query = f"""
    WITH cost_data AS (
      SELECT
        project.id AS project_id,
        service.description AS gcp_service,
        COALESCE(resource.name, '') AS service_name,
        SUM(cost) AS cost,
        SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS credits,
        SUM(cost) + SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS net_cost
      FROM `{table_fqn}`
      WHERE {where_clause}
      GROUP BY project_id, gcp_service, service_name
    )
    SELECT * FROM cost_data
    WHERE cost > 0.01 OR ABS(credits) > 0.01
    ORDER BY net_cost DESC, cost DESC
    LIMIT 50
    """

    totals_query = f"""
    SELECT
      SUM(cost) AS total_cost,
      SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)) AS total_credits
    FROM `{table_fqn}`
    WHERE {where_clause}
    """

    empty_totals = {"total_cost": 0.0, "total_credits": 0.0, "total_net_cost": 0.0}
    try:
        client = bigquery.Client(project=_PROJECT_ID)

        items = []
        for row in client.query(items_query).result():
            items.append(
                CostItem(
                    project_id=row.project_id or _PROJECT_ID,
                    service_name=row.service_name or "",
                    gcp_service=row.gcp_service or "",
                    cost=round(float(row.cost), 4),
                    credits=round(float(row.credits), 4),
                    net_cost=round(float(row.net_cost), 4),
                )
            )

        totals = dict(empty_totals)
        for row in client.query(totals_query).result():
            total_cost = float(row.total_cost or 0.0)
            total_credits = float(row.total_credits or 0.0)
            totals = {
                "total_cost": total_cost,
                "total_credits": total_credits,
                "total_net_cost": total_cost + total_credits,
            }

        return items, totals
    except Exception as e:
        print(f"BigQuery query failed: {e}")
        return [], dict(empty_totals)


def build_cost_query(
    group_by: str = "service",
    days: int = 30,
) -> str:
    """Build a parametrized BigQuery cost query. Used by SQL templates."""
    table_fqn = (
        f"{_PROJECT_ID}.{_DATASET}.gcp_billing_export_resource_v1_01CBB5_464EAA_96C8AC"
    )

    group_clauses = {
        "project": "GROUP BY project_id, project_display_name",
        "service": "GROUP BY gcp_service, service_name, project_id",
        "sku": "GROUP BY sku_id, sku_description, gcp_service",
    }
    group_clause = group_clauses.get(group_by, "GROUP BY project_id, gcp_service")

    select_clauses = {
        "project": "SELECT project.id AS project_id, project.name AS project_display_name, SUM(cost) AS cost",
        "service": "SELECT service.description AS gcp_service, resource.name AS service_name, project.id AS project_id, SUM(cost) AS cost",
        "sku": "SELECT sku.id AS sku_id, sku.description AS sku_description, service.description AS gcp_service, SUM(cost) AS cost",
    }
    select_clause = select_clauses.get(
        group_by,
        "SELECT project.id AS project_id, service.description AS gcp_service, SUM(cost) AS cost",
    )

    where = f"\nWHERE _PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)"
    return f"{select_clause}\nFROM `{table_fqn}`{where}\n{group_clause}\nORDER BY cost DESC"


def get_cost_summary(days: int = 30, month_to_date: bool = False) -> CostSummary:
    """Cost summary for a window.

    - ``month_to_date=True``: current calendar month, matching the GCP billing
      console's "current month" widget. Uses ``invoice.month`` in the billing
      account timezone (America/Los_Angeles), so ``days`` is ignored.
    - otherwise: a rolling window of the last ``days`` days (by ``_PARTITIONTIME``).
    """
    today = date.today()

    if month_to_date:
        where_clause = (
            "invoice.month = FORMAT_DATE('%Y%m', CURRENT_DATE('America/Los_Angeles'))\n"
            "        AND cost_type = 'regular'"
        )
        period = CostPeriod(
            start=today.replace(day=1).isoformat(),
            end=today.isoformat(),
        )
    else:
        where_clause = (
            f"_PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)\n"
            "        AND cost_type = 'regular'"
        )
        period = CostPeriod(
            start=(today - timedelta(days=days)).isoformat(),
            end=today.isoformat(),
        )

    table = _billing_table_exists()
    if table:
        items, totals = _query_billing(table, where_clause)
    else:
        items = []
        totals = {"total_cost": 0.0, "total_credits": 0.0, "total_net_cost": 0.0}

    return CostSummary(
        currency="USD",
        period=period,
        total_cost=round(totals["total_cost"], 2),
        total_credits=round(totals["total_credits"], 2),
        total_net_cost=round(totals["total_net_cost"], 2),
        items=items,
    )


def get_billing_status() -> dict:
    """Return billing export status for UI display."""
    table = _billing_table_exists()
    row_count = 0
    if table:
        try:
            client = bigquery.Client(project=_PROJECT_ID)
            rows = client.query(
                f"SELECT COUNT(*) AS n FROM `{table}` WHERE _PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)"
            ).result()
            for row in rows:
                row_count = row.n
        except Exception:
            pass

    return {
        "billing_export_enabled": table is not None,
        "bigquery_table": table or "",
        "dataset": f"{_PROJECT_ID}.{_DATASET}",
        "row_count": row_count,
        "is_estimate": row_count == 0,
        "message": (
            f"Real cost data available ({row_count} rows)"
            if row_count > 0
            else "Billing export table exists but no data yet. First sync takes 24-48h after enablement."
        )
        if table
        else "Billing export not enabled. Enable in GCP Console → Billing → BigQuery Export.",
    }


def get_cost_by_service(days: int = 30, month_to_date: bool = False) -> CostSummary:
    return get_cost_summary(days=days, month_to_date=month_to_date)
