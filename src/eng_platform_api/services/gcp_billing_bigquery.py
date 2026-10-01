"""Exported consumption costs; missing or delayed billing is never estimated zero."""

from __future__ import annotations

import time
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from google.cloud import bigquery

from ..config import config
from ..models import (
    BillingQuality,
    CostChange,
    CostComponentCoverage,
    CostComparison,
    CostItem,
    CostPeriod,
    CostSummary,
    DailyCost,
    DailyCostSeries,
)

_PROJECT_ID = "cgm-assistant-prod"
_DATASET = "billing_export"
_TIMEZONE = ZoneInfo("America/Bogota")
_TABLE_CACHE_TTL_SECONDS = 600
_table_cache: tuple[float, str] | None = None
_CREDITS_SUM = "SUM(COALESCE((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0))"
_ITEMS_DIMENSIONS = {
    "resource": (
        "project.id AS project_id, service.description AS gcp_service, "
        "COALESCE(resource.name, '') AS service_name",
        "GROUP BY project_id, gcp_service, service_name, currency",
    ),
    "service": (
        "project.id AS project_id, service.description AS gcp_service, '' AS service_name",
        "GROUP BY project_id, gcp_service, currency",
    ),
    "sku": (
        "project.id AS project_id, service.description AS gcp_service, "
        "COALESCE(sku.description, '') AS service_name",
        "GROUP BY project_id, gcp_service, service_name, currency",
    ),
}


class BillingUnavailable(Exception):
    """Safe error marker; provider diagnostics may contain sensitive values."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _windows(days: int, month_to_date: bool, now: datetime):
    if not 1 <= days <= 365:
        raise ValueError("days must be between 1 and 365")
    local = now.astimezone(_TIMEZONE)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if month_to_date:
        start = midnight.replace(day=1)
        previous_start = (start - timedelta(days=1)).replace(day=1)
        previous_limit = start
    else:
        start = midnight - timedelta(days=days - 1)
        previous_start = start - timedelta(days=days)
        previous_limit = start
    duration = min(local - start, previous_limit - previous_start)
    return start, local, previous_start, previous_start + duration


def _period(start: datetime, end: datetime) -> CostPeriod:
    last = end - timedelta(microseconds=1) if end > start else end
    return CostPeriod(start=start.date().isoformat(), end=last.date().isoformat())


def _where(start: datetime, end: datetime, as_of: datetime) -> str:
    # Values are server-created aware datetimes, never caller SQL. Ingestion
    # partitions/invoice month are deliberately not consumption boundaries.
    return (
        f"project.id = '{_PROJECT_ID}' AND cost_type = 'regular' "
        f"AND usage_start_time >= TIMESTAMP('{start.isoformat()}') "
        f"AND usage_start_time < TIMESTAMP('{end.isoformat()}') "
        f"AND usage_end_time <= TIMESTAMP('{end.isoformat()}') "
        f"AND export_time <= TIMESTAMP('{as_of.isoformat()}')"
    )


def _billing_table_exists() -> str | None:
    global _table_cache
    if config.mock_mode:
        return None
    now = time.monotonic()
    if _table_cache and now - _table_cache[0] < _TABLE_CACHE_TTL_SECONDS:
        return _table_cache[1]
    try:
        client = bigquery.Client(project=_PROJECT_ID)
        for table in client.list_tables(client.dataset(_DATASET), max_results=100):
            if table.table_id.startswith("gcp_billing_export_resource_v1_"):
                result = f"{_PROJECT_ID}.{_DATASET}.{table.table_id}"
                _table_cache = (now, result)
                return result
    except Exception:
        return None
    return None


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def _build_items_sql(
    table_fqn: str, where_clause: str, group_by: str = "resource"
) -> str:
    dimensions, grouping = _ITEMS_DIMENSIONS[group_by]
    # Preserve coverage for each resource/SKU before aggregating any display
    # dimension. One exported component cannot hide another component's lag.
    return f"""
    WITH components AS (
      SELECT {dimensions}, currency,
        TO_JSON_STRING(STRUCT(COALESCE(resource.name, '') AS resource_name,
          COALESCE(sku.id, '') AS sku_id)) AS component_id,
        LOGICAL_AND(COALESCE(resource.name, '') != '' AND COALESCE(sku.id, '') != '') AS attributed,
        SUM(cost) AS cost, {_CREDITS_SUM} AS credits,
        MIN(usage_start_time) AS first_usage_at, MAX(usage_end_time) AS latest_usage_at,
        COUNT(DISTINCT TIMESTAMP_TRUNC(usage_start_time, HOUR)) AS observed_hours
      FROM `{table_fqn}` WHERE {where_clause}
      {grouping}, component_id
    )
    SELECT project_id, gcp_service, service_name, currency,
      SUM(cost) AS cost, SUM(credits) AS credits,
      SUM(cost) + SUM(credits) AS net_cost,
      MIN(first_usage_at) AS first_usage_at, MAX(latest_usage_at) AS latest_usage_at,
      MAX(observed_hours) AS observed_hours,
      ARRAY_AGG(STRUCT(component_id, attributed, first_usage_at, latest_usage_at, observed_hours)) AS components
    FROM components GROUP BY project_id, gcp_service, service_name, currency
    ORDER BY net_cost DESC, cost DESC
    """


def _query_billing(table_fqn: str, where_clause: str, group_by: str = "resource"):
    try:
        client = bigquery.Client(project=_PROJECT_ID)
        items = [
            CostItem(
                project_id=row.project_id or _PROJECT_ID,
                service_name=row.service_name or "",
                gcp_service=row.gcp_service or "",
                cost=round(float(row.cost), 6),
                credits=round(float(row.credits), 6),
                net_cost=round(float(row.net_cost), 6),
                currency=getattr(row, "currency", "USD"),
                attributed=bool(row.service_name) if group_by == "resource" else True,
                first_usage_at=_iso(getattr(row, "first_usage_at", None)),
                latest_usage_at=_iso(getattr(row, "latest_usage_at", None)),
                observed_hours=int(getattr(row, "observed_hours", 0)),
                components=[
                    CostComponentCoverage(
                        component_id=c["component_id"],
                        attributed=bool(c["attributed"]),
                        first_usage_at=_iso(c["first_usage_at"]),
                        latest_usage_at=_iso(c["latest_usage_at"]),
                        observed_hours=int(c["observed_hours"]),
                    )
                    for c in (getattr(row, "components", None) or [])
                ],
            )
            for row in client.query(
                _build_items_sql(table_fqn, where_clause, group_by)
            ).result()
        ]
        sql = f"""
        SELECT SUM(cost) AS total_cost, {_CREDITS_SUM} AS total_credits,
          COUNT(*) AS n, ARRAY_AGG(DISTINCT currency IGNORE NULLS) AS currencies,
          MAX(export_time) AS latest_export_at,
          MIN(usage_start_time) AS first_usage_at, MAX(usage_end_time) AS latest_usage_at
        FROM `{table_fqn}` WHERE {where_clause}
        """
        totals = next(iter(client.query(sql).result()))
        return items, totals
    except Exception:
        raise BillingUnavailable("billing_query_unavailable") from None


def _summary(
    table: str | None, start: datetime, end: datetime, as_of: datetime, group_by: str
) -> CostSummary:
    quality = BillingQuality(retrieved_at=as_of.isoformat())
    result = CostSummary(period=_period(start, end), data_quality=quality)
    if not table:
        quality.status, quality.reason = "unavailable", "export_unavailable"
        return result
    try:
        items, row = _query_billing(table, _where(start, end, as_of), group_by)
    except BillingUnavailable:
        quality.status, quality.reason = "unavailable", "billing_query_unavailable"
        return result
    quality.rows = int(getattr(row, "n", 0))
    quality.latest_export_at = _iso(getattr(row, "latest_export_at", None))
    quality.first_usage_at = _iso(getattr(row, "first_usage_at", None))
    quality.latest_usage_at = _iso(getattr(row, "latest_usage_at", None))
    currencies = getattr(row, "currencies", None) or []
    result.items = items
    if not quality.rows:
        quality.reason = "no_exported_usage_in_window"
        return result
    if len(currencies) != 1:
        quality.status, quality.reason = "mixed_currency", "cannot_sum_currencies"
        result.currency = "mixed"
        return result
    result.currency = currencies[0]
    quality.status, quality.reason = "partial", "export_is_provisional"
    result.total_cost = round(float(row.total_cost or 0), 6)
    result.total_credits = round(float(row.total_credits or 0), 6)
    result.total_net_cost = round(result.total_cost + result.total_credits, 6)
    return result


def get_cost_summary(
    days: int = 30, month_to_date: bool = False, group_by: str = "resource"
) -> CostSummary:
    now = utc_now()
    start, end, _, _ = _windows(days, month_to_date, now)
    return _summary(_billing_table_exists(), start, end, now, group_by)


def get_cost_by_service(days: int = 30, month_to_date: bool = False) -> CostSummary:
    return get_cost_summary(days, month_to_date, "service")


def get_cost_by_sku(days: int = 30, month_to_date: bool = False) -> CostSummary:
    return get_cost_summary(days, month_to_date, "sku")


def build_cost_query(group_by: str = "service", days: int = 30) -> str:
    now = utc_now()
    start, end, _, _ = _windows(days, False, now)
    table = (
        f"{_PROJECT_ID}.{_DATASET}.gcp_billing_export_resource_v1_01CBB5_464EAA_96C8AC"
    )
    if group_by == "project":
        return (
            f"SELECT project.id AS project_id, SUM(cost) AS cost FROM `{table}` "
            f"WHERE {_where(start, end, now)} GROUP BY project_id"
        )
    return _build_items_sql(table, _where(start, end, now), group_by)


def _split_daily_rows(
    rows: list[tuple[date, float, float]], current: CostPeriod, previous: CostPeriod
) -> tuple[list[DailyCost], float | None]:
    current_start, current_end = (
        date.fromisoformat(current.start),
        date.fromisoformat(current.end),
    )
    previous_start, previous_end = (
        date.fromisoformat(previous.start),
        date.fromisoformat(previous.end),
    )
    by_day: dict[date, tuple[float, float]] = {}
    previous_values = []
    for day, cost, credits in rows:
        if current_start <= day <= current_end:
            old = by_day.get(day, (0.0, 0.0))
            by_day[day] = old[0] + cost, old[1] + credits
        elif previous_start <= day <= previous_end:
            previous_values.append(cost + credits)
    series = []
    day = current_start
    while day <= current_end:
        values = by_day.get(day)
        series.append(
            DailyCost(
                date=day.isoformat(),
                has_data=values is not None,
                cost=round(values[0], 6) if values else None,
                credits=round(values[1], 6) if values else None,
                net_cost=round(sum(values), 6) if values else None,
            )
        )
        day += timedelta(days=1)
    return series, round(sum(previous_values), 6) if previous_values else None


def get_daily_costs(days: int = 30, month_to_date: bool = False) -> DailyCostSeries:
    now = utc_now()
    start, end, prev_start, prev_end = _windows(days, month_to_date, now)
    current, previous = _period(start, end), _period(prev_start, prev_end)
    table = _billing_table_exists()
    summary = _summary(table, prev_start, end, now, "service")
    quality = summary.data_quality
    rows = []
    if table and quality.status == "partial":
        sql = f"""
        SELECT DATE(usage_start_time, 'America/Bogota') AS usage_date,
          SUM(cost) AS cost, {_CREDITS_SUM} AS credits
        FROM `{table}` WHERE ({_where(start, end, now)}) OR ({_where(prev_start, prev_end, now)})
        GROUP BY usage_date ORDER BY usage_date
        """
        try:
            client = bigquery.Client(project=_PROJECT_ID)
            rows = [
                (r.usage_date, float(r.cost or 0), float(r.credits or 0))
                for r in client.query(sql).result()
            ]
        except Exception:
            quality.status, quality.reason = "unavailable", "billing_query_unavailable"
    series, prev_total = _split_daily_rows(rows, current, previous)
    # The plotted current series keeps all requested observed rows. Its prior
    # total is only comparable if the full requested cut has verified equal
    # coverage; otherwise expose unknown instead of a misleading decrease.
    comparison = _comparison(days, month_to_date, "service", now)
    comparable = (
        quality.status == "partial"
        and comparison.comparable
        and comparison.current_start_at == start.isoformat()
        and comparison.current_end_at == end.isoformat()
        and comparison.previous_start_at == prev_start.isoformat()
        and comparison.previous_end_at == prev_end.isoformat()
    )
    return DailyCostSeries(
        currency=summary.currency,
        period=current,
        days=series,
        previous_period=previous,
        previous_total_net_cost=prev_total if comparable else None,
        previous_comparable=comparable,
        previous_comparison_reason="equivalent_exported_windows_provisional"
        if comparable
        else "incomplete_or_unequal_daily_coverage",
        data_quality=quality,
    )


def _timestamp(value: str | None) -> datetime | None:
    try:
        result = datetime.fromisoformat(value) if value else None
        return result if result and result.tzinfo else None
    except ValueError:
        return None


def _matched_item(
    current: CostItem | None,
    previous: CostItem | None,
    start: datetime,
    end: datetime,
    prev_start: datetime,
    prev_end: datetime,
):
    exemplar = current or previous
    if exemplar is None:
        raise ValueError("A comparison item requires an observed resource")
    change = CostChange(
        project_id=exemplar.project_id,
        service_name=exemplar.service_name,
        gcp_service=exemplar.gcp_service,
        currency=exemplar.currency,
        current=current,
        previous=previous,
    )
    if not current or not previous:
        return change
    if current.currency != previous.currency:
        change.reason = "currency_mismatch"
        return change
    for item, begin, stop in [(current, start, end), (previous, prev_start, prev_end)]:
        first, last = _timestamp(item.first_usage_at), _timestamp(item.latest_usage_at)
        if (
            first is None
            or last is None
            or first > begin
            or last < stop
            or item.observed_hours < math.ceil((stop - begin).total_seconds() / 3600)
        ):
            change.reason = "incomplete_resource_coverage"
            return change
    current_components = {c.component_id: c for c in current.components}
    previous_components = {c.component_id: c for c in previous.components}
    if (
        not current_components
        or current_components.keys() != previous_components.keys()
    ):
        change.reason = "missing_or_changed_component_coverage"
        return change
    for components, begin, stop in [
        (current_components, start, end),
        (previous_components, prev_start, prev_end),
    ]:
        for component in components.values():
            first, last = (
                _timestamp(component.first_usage_at),
                _timestamp(component.latest_usage_at),
            )
            if (
                not component.attributed
                or first is None
                or last is None
                or first > begin
                or last < stop
                or component.observed_hours
                < math.ceil((stop - begin).total_seconds() / 3600)
            ):
                change.reason = "incomplete_component_coverage"
                return change
    change.comparable, change.reason = True, "equivalent_exported_windows_provisional"
    change.net_change = round(current.net_cost - previous.net_cost, 6)
    if previous.net_cost:
        change.percent_change = round(
            change.net_change / abs(previous.net_cost) * 100, 4
        )
    return change


def get_cost_comparison(
    days: int = 1, month_to_date: bool = False, group_by: str = "resource"
) -> CostComparison:
    return _comparison(days, month_to_date, group_by, utc_now())


def _comparison(
    days: int, month_to_date: bool, group_by: str, now: datetime
) -> CostComparison:
    start, end, prev_start, prev_end = _windows(days, month_to_date, now)
    table = _billing_table_exists()
    duration = min(end - start, prev_end - prev_start)
    end, prev_end = start + duration, prev_start + duration
    current = _summary(table, start, end, now, group_by)
    previous = _summary(table, prev_start, prev_end, now, group_by)
    # Equal elapsed local time, bounded by the least observed usage watermark.
    latest, prev_latest = (
        _timestamp(s.data_quality.latest_usage_at) for s in [current, previous]
    )
    if latest and prev_latest:
        duration = max(
            timedelta(0),
            min(
                end - start,
                prev_end - prev_start,
                latest - start,
                prev_latest - prev_start,
            ),
        )
        end, prev_end = start + duration, prev_start + duration
        current = _summary(table, start, end, now, group_by)
        previous = _summary(table, prev_start, prev_end, now, group_by)

    def key(i):
        return i.project_id, i.gcp_service, i.service_name, i.currency

    curr, prev = ({key(i): i for i in s.items} for s in [current, previous])
    items = [
        _matched_item(curr.get(k), prev.get(k), start, end, prev_start, prev_end)
        for k in sorted(curr.keys() | prev.keys())
    ]
    comparable = (
        bool(items)
        and all(i.comparable for i in items)
        and all(s.data_quality.status == "partial" for s in [current, previous])
    )
    return CostComparison(
        current=current,
        previous=previous,
        current_start_at=start.isoformat(),
        current_end_at=end.isoformat(),
        previous_start_at=prev_start.isoformat(),
        previous_end_at=prev_end.isoformat(),
        items=items,
        comparable=comparable,
        net_change=round(sum(i.net_change or 0 for i in items), 6)
        if comparable
        else None,
        reason="equivalent_exported_windows_provisional"
        if comparable
        else "incomplete_or_missing_data",
    )


def get_billing_status() -> dict:
    result = get_cost_summary(days=30)
    quality = result.data_quality
    return {
        "billing_export_enabled": quality.reason != "export_unavailable",
        "dataset": f"{_PROJECT_ID}.{_DATASET}",
        "row_count": quality.rows,
        "is_estimate": False,
        "data_quality": quality.model_dump(mode="json"),
        "message": "Exported consumption costs are provisional; this is not realtime pricing.",
    }
