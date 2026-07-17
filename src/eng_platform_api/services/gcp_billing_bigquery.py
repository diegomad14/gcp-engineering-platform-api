"""BigQuery billing service — cost queries with fallback to estimates.

Requires Cloud Billing Export enabled in GCP Console:
https://console.cloud.google.com/billing/01CBB5-464EAA-96C8AC
"""

import time
from datetime import date, timedelta
from typing import Optional

from google.cloud import bigquery

from ..config import config
from ..models import CostItem, CostPeriod, CostSummary, DailyCost, DailyCostSeries

_PROJECT_ID = "cgm-assistant-prod"
_DATASET = "billing_export"

_TABLE_CACHE_TTL_SECONDS = 600
_table_cache: Optional[tuple[float, str]] = None


def _billing_table_exists() -> Optional[str]:
    """Check if billing export table exists and return its full ID.

    Successful lookups are memoized for 10 minutes: the table name never
    changes once the export is enabled, and `list_tables` would otherwise run
    on every request. Misses are not cached so a transient failure recovers
    on the next request.
    """
    global _table_cache
    if config.mock_mode:
        return None

    now = time.monotonic()
    if _table_cache and now - _table_cache[0] < _TABLE_CACHE_TTL_SECONDS:
        return _table_cache[1]

    try:
        client = bigquery.Client(project=_PROJECT_ID)
        dataset_ref = client.dataset(_DATASET)
        tables = list(client.list_tables(dataset_ref, max_results=10))
        for table in tables:
            table_id = table.table_id
            if table_id.startswith("gcp_billing_export"):
                table_fqn = f"{_PROJECT_ID}.{_DATASET}.{table_id}"
                _table_cache = (now, table_fqn)
                return table_fqn
        return None
    except Exception:
        return None


_CREDITS_SUM = "SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0))"

_ITEMS_DIMENSIONS = {
    # Per-resource rows (Cloud Run service, SQL instance, secret, …).
    "resource": (
        "project.id AS project_id,\n"
        "        service.description AS gcp_service,\n"
        "        COALESCE(resource.name, '') AS service_name",
        "GROUP BY project_id, gcp_service, service_name",
    ),
    # One row per GCP service (Cloud Run, Cloud SQL, …).
    "service": (
        "project.id AS project_id,\n"
        "        service.description AS gcp_service,\n"
        "        '' AS service_name",
        "GROUP BY project_id, gcp_service",
    ),
    # One row per SKU; the SKU description takes the service_name slot so the
    # response shape stays CostSummary.
    "sku": (
        "project.id AS project_id,\n"
        "        service.description AS gcp_service,\n"
        "        COALESCE(sku.description, '') AS service_name",
        "GROUP BY project_id, gcp_service, service_name",
    ),
}


def _build_items_sql(
    table_fqn: str, where_clause: str, group_by: str = "resource"
) -> str:
    """Build the line-items query for one of the `_ITEMS_DIMENSIONS` groupings."""
    select_dims, group_clause = _ITEMS_DIMENSIONS[group_by]
    return f"""
    WITH cost_data AS (
      SELECT
        {select_dims},
        SUM(cost) AS cost,
        {_CREDITS_SUM} AS credits,
        SUM(cost) + {_CREDITS_SUM} AS net_cost
      FROM `{table_fqn}`
      WHERE {where_clause}
      {group_clause}
    )
    SELECT * FROM cost_data
    WHERE cost > 0.01 OR ABS(credits) > 0.01
    ORDER BY net_cost DESC, cost DESC
    LIMIT 50
    """


def _query_billing(
    table_fqn: str, where_clause: str, group_by: str = "resource"
) -> tuple[list[CostItem], dict]:
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
    the ``cost_type = 'regular'`` guard. ``group_by`` picks the line-item
    dimension (see `_ITEMS_DIMENSIONS`); totals are independent of it.
    """
    items_query = _build_items_sql(table_fqn, where_clause, group_by)

    totals_query = f"""
    SELECT
      SUM(cost) AS total_cost,
      {_CREDITS_SUM} AS total_credits
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
    """Build a parametrized BigQuery cost query. Used by SQL templates.

    Kept as a documented template; the live endpoints go through
    :func:`_build_items_sql` / :func:`_query_billing` instead.
    """
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


def get_cost_summary(
    days: int = 30, month_to_date: bool = False, group_by: str = "resource"
) -> CostSummary:
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
        items, totals = _query_billing(table, where_clause, group_by)
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
    return get_cost_summary(days=days, month_to_date=month_to_date, group_by="service")


def get_cost_by_sku(days: int = 30, month_to_date: bool = False) -> CostSummary:
    return get_cost_summary(days=days, month_to_date=month_to_date, group_by="sku")


# ── Daily series ─────────────────────────────────────────────────────


def _split_daily_rows(
    rows: list[tuple[date, float, float]],
    current: CostPeriod,
    previous: CostPeriod,
) -> tuple[list[DailyCost], float]:
    """Partition ``(usage_date, cost, credits)`` rows into the two windows.

    Returns the current window as a gap-free day series (missing days filled
    with zeros) plus the previous window's total net cost. Rows outside both
    windows (billing-export ingest lag can spill a day either side) are
    dropped.
    """
    current_start = date.fromisoformat(current.start)
    current_end = date.fromisoformat(current.end)
    previous_start = date.fromisoformat(previous.start)
    previous_end = date.fromisoformat(previous.end)

    by_day: dict[date, tuple[float, float]] = {}
    previous_total = 0.0
    for usage_date, cost, credits in rows:
        if current_start <= usage_date <= current_end:
            prev_cost, prev_credits = by_day.get(usage_date, (0.0, 0.0))
            by_day[usage_date] = (prev_cost + cost, prev_credits + credits)
        elif previous_start <= usage_date <= previous_end:
            previous_total += cost + credits

    series = []
    day = current_start
    while day <= current_end:
        cost, credits = by_day.get(day, (0.0, 0.0))
        series.append(
            DailyCost(
                date=day.isoformat(),
                cost=round(cost, 4),
                credits=round(credits, 4),
                net_cost=round(cost + credits, 4),
            )
        )
        day += timedelta(days=1)

    return series, round(previous_total, 2)


def get_daily_costs(days: int = 30, month_to_date: bool = False) -> DailyCostSeries:
    """Daily net cost for the window plus the previous window's total.

    One query covers both windows (current + previous) grouped by usage date
    in the billing account timezone; the split happens in Python. Note the
    rolling window filters by ``_PARTITIONTIME`` (UTC ingest time) while days
    group by ``usage_start_time`` (America/Los_Angeles), so edges can differ
    ±1 day from ``/summary`` — accepted, not reconciled to the cent.
    """
    today = date.today()

    if month_to_date:
        first_of_month = today.replace(day=1)
        prev_month_end = first_of_month - timedelta(days=1)
        current = CostPeriod(start=first_of_month.isoformat(), end=today.isoformat())
        previous = CostPeriod(
            start=prev_month_end.replace(day=1).isoformat(),
            end=prev_month_end.isoformat(),
        )
        where_clause = (
            "invoice.month IN (\n"
            "          FORMAT_DATE('%Y%m', CURRENT_DATE('America/Los_Angeles')),\n"
            "          FORMAT_DATE('%Y%m', DATE_SUB(DATE_TRUNC(CURRENT_DATE('America/Los_Angeles'), MONTH), INTERVAL 1 DAY))\n"
            "        )\n"
            "        AND cost_type = 'regular'"
        )
    else:
        current = CostPeriod(
            start=(today - timedelta(days=days)).isoformat(),
            end=today.isoformat(),
        )
        previous = CostPeriod(
            start=(today - timedelta(days=2 * days)).isoformat(),
            end=(today - timedelta(days=days + 1)).isoformat(),
        )
        where_clause = (
            f"_PARTITIONTIME >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {2 * days} DAY)\n"
            "        AND cost_type = 'regular'"
        )

    table = _billing_table_exists()
    if not table:
        return DailyCostSeries(
            currency="USD",
            period=current,
            days=[],
            previous_period=previous,
            previous_total_net_cost=0.0,
        )

    rows: list[tuple[date, float, float]] = []
    if table:
        daily_query = f"""
        SELECT
          DATE(usage_start_time, 'America/Los_Angeles') AS usage_date,
          SUM(cost) AS cost,
          {_CREDITS_SUM} AS credits
        FROM `{table}`
        WHERE {where_clause}
        GROUP BY usage_date
        ORDER BY usage_date
        """
        try:
            client = bigquery.Client(project=_PROJECT_ID)
            for row in client.query(daily_query).result():
                rows.append(
                    (row.usage_date, float(row.cost or 0.0), float(row.credits or 0.0))
                )
        except Exception as e:
            print(f"BigQuery daily query failed: {e}")
            rows = []

    series, previous_total = _split_daily_rows(rows, current, previous)

    return DailyCostSeries(
        currency="USD",
        period=current,
        days=series,
        previous_period=previous,
        previous_total_net_cost=previous_total,
    )
