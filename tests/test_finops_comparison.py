"""Consumption-time and incomplete-export regressions; all queries stay offline."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import re
from zoneinfo import ZoneInfo

import pytest
from google.cloud.bigquery import Row, SchemaField, _helpers

from eng_platform_api.services import gcp_billing_bigquery as billing
from eng_platform_api.routers import costs

UTC = timezone.utc
NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
COMPONENT_SCHEMA = [
    SchemaField("component_id", "STRING"),
    SchemaField("attributed", "BOOLEAN"),
    SchemaField("first_usage_at", "TIMESTAMP"),
    SchemaField("latest_usage_at", "TIMESTAMP"),
    SchemaField("observed_hours", "INTEGER"),
]


def sdk_result_rows(rows):
    """Decode nested REST records with BigQuery's actual result converter."""
    result = []
    for row in rows:
        values = vars(row).copy()
        if "components" in values:
            cells = [
                {
                    "v": {
                        "f": [
                            {"v": c.component_id},
                            {"v": str(c.attributed).lower()},
                            {"v": str(int(c.first_usage_at.timestamp() * 1_000_000))},
                            {"v": str(int(c.latest_usage_at.timestamp() * 1_000_000))},
                            {"v": str(c.observed_hours)},
                        ]
                    }
                }
                for c in values["components"]
            ]
            values["components"] = _helpers._rows_from_json(
                [{"f": [{"v": cells}]}],
                [
                    SchemaField(
                        "components", "RECORD", mode="REPEATED", fields=COMPONENT_SCHEMA
                    )
                ],
            )[0].components
        result.append(
            Row(tuple(values.values()), {name: i for i, name in enumerate(values)})
        )
    return result


def records(
    day,
    hours,
    cost=1,
    currency="USD",
    resource="db",
    service="Cloud SQL",
    sku="compute",
):
    start = datetime.fromisoformat(day).replace(hour=5, tzinfo=UTC)
    return [
        dict(
            start=start + timedelta(hours=h),
            end=start + timedelta(hours=h + 1),
            exported=NOW - timedelta(minutes=10),
            cost=cost,
            credits=-cost / 4,
            currency=currency,
            resource=resource,
            service=service,
            sku=sku,
        )
        for h in range(hours)
    ]


class OfflineBilling:
    def __init__(self, rows):
        self.rows, self.queries = rows, []
        self.fail = False

    def dataset(self, name):
        return name

    def list_tables(self, ref, max_results):
        return [SimpleNamespace(table_id="gcp_billing_export_resource_v1_TEST")]

    def query(self, sql):
        self.queries.append(sql)
        if self.fail:
            raise RuntimeError("sensitive-provider-token")
        dates = [
            datetime.fromisoformat(d)
            for d in re.findall(r"TIMESTAMP\('([^']+)'\)", sql)
        ]
        windows = [dates[i : i + 4] for i in range(0, len(dates), 4)]
        selected = [
            r
            for r in self.rows
            if any(
                a <= r["start"] < b and r["end"] <= c and r["exported"] <= d
                for a, b, c, d in windows
            )
        ]
        if "COUNT(*) AS n" in sql:
            out = [
                SimpleNamespace(
                    total_cost=sum(r["cost"] for r in selected),
                    total_credits=sum(r["credits"] for r in selected),
                    n=len(selected),
                    currencies=sorted({r["currency"] for r in selected}),
                    latest_export_at=max(
                        (r["exported"] for r in selected), default=None
                    ),
                    first_usage_at=min((r["start"] for r in selected), default=None),
                    latest_usage_at=max((r["end"] for r in selected), default=None),
                )
            ]
        else:
            groups = {}
            for r in selected:
                key = (
                    r["start"].astimezone(ZoneInfo("America/Bogota")).date()
                    if "usage_date" in sql
                    else (
                        ""
                        if "'' AS service_name" in sql
                        else r["resource"]
                        if "COALESCE(resource.name, '') AS service_name" in sql
                        else r["sku"],
                        r["service"],
                        r["currency"],
                    )
                )
                groups.setdefault(key, []).append(r)
            out = []
            for key, values in groups.items():
                gross, credit = (
                    sum(r["cost"] for r in values),
                    sum(r["credits"] for r in values),
                )
                if "usage_date" in sql:
                    out.append(
                        SimpleNamespace(usage_date=key, cost=gross, credits=credit)
                    )
                else:
                    out.append(
                        SimpleNamespace(
                            project_id="cgm-assistant-prod",
                            service_name=key[0],
                            gcp_service=key[1],
                            currency=key[2],
                            cost=gross,
                            credits=credit,
                            net_cost=gross + credit,
                            first_usage_at=min(r["start"] for r in values),
                            latest_usage_at=max(r["end"] for r in values),
                            observed_hours=len({r["start"] for r in values}),
                            components=[
                                SimpleNamespace(
                                    component_id=resource + ":" + sku,
                                    attributed=bool(resource and sku),
                                    first_usage_at=min(
                                        r["start"]
                                        for r in values
                                        if (r["resource"], r["sku"]) == (resource, sku)
                                    ),
                                    latest_usage_at=max(
                                        r["end"]
                                        for r in values
                                        if (r["resource"], r["sku"]) == (resource, sku)
                                    ),
                                    observed_hours=len(
                                        {
                                            r["start"]
                                            for r in values
                                            if (r["resource"], r["sku"])
                                            == (resource, sku)
                                        }
                                    ),
                                )
                                for resource, sku in {
                                    (r["resource"], r["sku"]) for r in values
                                }
                            ],
                        )
                    )
        return SimpleNamespace(result=lambda: sdk_result_rows(out))


@pytest.fixture
def dataset(monkeypatch):
    client = OfflineBilling(records("2026-10-01", 4, 2) + records("2026-09-30", 7))
    monkeypatch.setattr(billing.config, "mock_mode", False)
    monkeypatch.setattr(billing, "_table_cache", None)
    monkeypatch.setattr(billing, "utc_now", lambda: NOW)
    monkeypatch.setattr(billing.bigquery, "Client", lambda project: client)
    return client


@pytest.mark.parametrize(
    "instant, expected",
    [
        ("2026-10-01T04:59:00+00:00", "2026-09-01"),
        ("2026-10-01T05:36:00+00:00", "2026-10-01"),
        ("2026-10-01T07:01:00+00:00", "2026-10-01"),
    ],
)
def test_rollover_uses_bogota_not_utc_or_los_angeles(instant, expected):
    start, end, prev_start, prev_end = billing._windows(
        7, True, datetime.fromisoformat(instant)
    )
    assert start.date().isoformat() == expected
    assert end.tzinfo == ZoneInfo("America/Bogota")
    assert prev_end - prev_start <= end - start


def test_latest_consumption_watermark_matches_previous_elapsed_hours(dataset):
    result = billing.get_cost_comparison()
    assert result.comparable
    assert result.current_end_at == "2026-10-01T04:00:00-05:00"
    assert result.previous_end_at == "2026-09-30T04:00:00-05:00"
    assert result.current.total_net_cost == 6
    assert result.previous.total_net_cost == 3
    assert result.net_change == 3
    assert result.current.data_quality.latest_export_at is not None
    assert not result.is_final and not result.current.data_quality.is_complete


def test_full_watermarks_do_not_repeat_identical_comparison_queries(dataset):
    dataset.rows = records("2026-10-01", 7, 2) + records("2026-09-30", 7)
    result = billing.get_cost_comparison()
    assert result.comparable and result.net_change == 5.25
    assert result.current_end_at == "2026-10-01T07:00:00-05:00"
    assert len(dataset.queries) == 4
    assert len(set(dataset.queries)) == 4


def test_short_watermarks_still_requery_both_comparison_windows(dataset):
    result = billing.get_cost_comparison()
    assert result.comparable and result.net_change == 3
    assert result.current_end_at == "2026-10-01T04:00:00-05:00"
    assert len(dataset.queries) == 8


@pytest.mark.parametrize("complete, expected_queries", [(True, 6), (False, 10)])
def test_daily_skips_unused_item_query_preserving_coverage(
    dataset, complete, expected_queries
):
    if complete:
        dataset.rows = records("2026-10-01", 7, 2) + records("2026-09-30", 7)
    result = billing.get_daily_costs(days=1)
    assert "COUNT(*) AS n" in dataset.queries[0]
    assert "usage_date" in dataset.queries[1]
    assert len(dataset.queries) == expected_queries
    assert result.days[0].net_cost == (10.5 if complete else 6)
    assert result.previous_comparable is complete
    assert result.previous_total_net_cost == (5.25 if complete else None)


@pytest.mark.parametrize("currency", ["USD", "COP"])
@pytest.mark.parametrize("group_by", ["resource", "service", "sku"])
def test_sdk_rows_reach_summary_and_comparison_dtos(
    dataset, monkeypatch, currency, group_by
):
    dataset.rows = records("2026-10-01", 4, 2, currency=currency) + records(
        "2026-09-30", 7, currency=currency
    )
    monkeypatch.setattr(costs.config.cloud_build, "usage_enabled", False)
    costs._cache.clear()
    loader = {
        "resource": costs.get_cost_summary,
        "service": costs.get_cost_by_service,
        "sku": costs.get_cost_by_sku,
    }[group_by]
    summary = loader(days=1, month_to_date=False).model_dump(mode="json")
    assert summary["data_quality"]["status"] == "partial"
    assert summary["data_quality"]["timezone"] == "America/Bogota"
    assert summary["currency"] == currency
    assert (
        summary["total_cost"],
        summary["total_credits"],
        summary["total_net_cost"],
    ) == (8, -2, 6)
    component = summary["items"][0]["components"][0]
    assert component["first_usage_at"] == "2026-10-01T05:00:00+00:00"
    assert component["latest_usage_at"] == "2026-10-01T09:00:00+00:00"
    assert component["observed_hours"] == 4 and component["attributed"]
    comparison = costs.get_cost_comparison(
        days=1, month_to_date=False, group_by=group_by
    ).model_dump(mode="json")
    assert comparison["comparable"]
    assert comparison["net_change"] == 3
    assert comparison["current"]["total_net_cost"] == 6
    assert comparison["previous"]["total_net_cost"] == 3
    assert (
        comparison["current"]["data_quality"]["latest_export_at"]
        == "2026-10-01T11:50:00+00:00"
    )
    costs._cache.clear()


def test_nested_components_also_accept_bigquery_row(dataset, monkeypatch):
    query = dataset.query

    def nested_rows(sql):
        result = list(query(sql).result())
        for row in result:
            if "components" in row.keys():
                for i, component in enumerate(row.components):
                    row.components[i] = Row(
                        tuple(component.values()),
                        {name: j for j, name in enumerate(component)},
                    )
        return SimpleNamespace(result=lambda: result)

    monkeypatch.setattr(dataset, "query", nested_rows)
    result = billing.get_cost_comparison()
    assert result.comparable and result.net_change == 3


def test_late_ingestion_is_not_a_consumption_boundary(dataset):
    # An old consumption hour exported today still belongs to its usage window.
    row = records("2026-09-30", 1, 4)[0]
    row["exported"] = NOW - timedelta(minutes=1)
    dataset.rows.append(row)
    result = billing.get_cost_comparison()
    assert result.previous.total_net_cost == 6
    assert result.net_change == 0
    assert all(
        "_PARTITIONTIME" not in q and "invoice.month" not in q for q in dataset.queries
    )
    assert all(
        "usage_start_time" in q and "export_time <=" in q for q in dataset.queries
    )


def test_month_summary_cannot_label_september_as_october(dataset, monkeypatch):
    dataset.rows = records("2026-09-30", 7, 49.62)
    monkeypatch.setattr(
        billing, "utc_now", lambda: datetime(2026, 10, 1, 5, 36, tzinfo=UTC)
    )
    summary = billing.get_cost_summary(month_to_date=True)
    assert summary.period.start == "2026-10-01"
    assert summary.total_cost is None
    assert summary.data_quality.status == "no_data"


def test_query_error_is_unknown_without_provider_secrets(dataset, capsys):
    dataset.fail = True
    summary = billing.get_cost_summary()
    assert summary.total_cost is None and summary.data_quality.status == "unavailable"
    result = billing.get_cost_comparison()
    assert not result.comparable and result.net_change is None
    assert "sensitive-provider-token" not in capsys.readouterr().out


def test_missing_resource_is_not_a_drop_to_zero(dataset):
    dataset.rows += records("2026-09-30", 4, resource="missing-today")
    result = billing.get_cost_comparison()
    missing = next(i for i in result.items if i.service_name == "missing-today")
    assert (
        missing.current is None
        and not missing.comparable
        and missing.net_change is None
    )
    assert result.net_change is None


def test_gap_in_hourly_coverage_blocks_false_savings(dataset):
    dataset.rows = [
        r for r in dataset.rows if not (r["start"].day == 1 and r["start"].hour == 7)
    ]
    result = billing.get_cost_comparison()
    assert not result.items[0].comparable
    assert result.items[0].reason == "incomplete_resource_coverage"


def test_currency_credit_and_unattributed_resource(dataset):
    dataset.rows = records("2026-10-01", 4, 2, resource="") + records(
        "2026-09-30", 4, resource=""
    )
    result = billing.get_cost_comparison()
    assert result.items[0].current.attributed is False
    assert result.current.total_net_cost == 6  # credit-adjusted, not gross 8
    assert result.net_change is None  # resource attribution is insufficient
    dataset.rows += records("2026-10-01", 1, currency="COP")
    summary = billing.get_cost_summary()
    assert summary.currency == "mixed" and summary.total_cost is None
    assert summary.data_quality.status == "mixed_currency"
    assert billing.get_cost_comparison().net_change is None


def test_daily_exactly_seven_dates_missing_is_unknown(dataset):
    daily = billing.get_daily_costs(days=7)
    assert len(daily.days) == 7
    assert daily.days[0].net_cost is None and not daily.days[0].has_data
    assert daily.days[-1].net_cost == 6
    assert daily.previous_period.end < daily.period.start


def test_cache_does_not_cross_bogota_midnight(monkeypatch):
    monkeypatch.setattr(costs.config, "mock_mode", False)
    costs._cache.clear()
    now = [datetime(2026, 10, 1, 4, 59, tzinfo=UTC)]
    monkeypatch.setattr(billing, "utc_now", lambda: now[0])
    assert costs._cached(("test",), lambda: "september") == "september"
    now[0] = datetime(2026, 10, 1, 5, 1, tzinfo=UTC)
    assert costs._cached(("test",), lambda: "october") == "october"
    costs._cache.clear()


def test_month_comparison_caps_short_previous_month(dataset, monkeypatch):
    monkeypatch.setattr(
        billing, "utc_now", lambda: datetime(2026, 3, 31, 12, tzinfo=UTC)
    )
    start, end, prev_start, prev_end = billing._windows(30, True, billing.utc_now())
    assert end.day == 31  # summary retains all requested current usage
    assert prev_end - prev_start == timedelta(days=28)


@pytest.mark.parametrize("days", [0, 366])
def test_service_rejects_unbounded_days(days):
    with pytest.raises(ValueError):
        billing.get_cost_summary(days=days)


def test_daily_currency_is_preserved_and_query_failure_stays_unknown(
    dataset, monkeypatch
):
    dataset.rows = records("2026-10-01", 4, currency="COP") + records(
        "2026-09-30", 4, currency="COP"
    )
    assert billing.get_daily_costs(days=1).currency == "COP"
    query = dataset.query

    def fail_daily(sql):
        if "usage_date" in sql:
            raise RuntimeError("provider-private-content")
        return query(sql)

    monkeypatch.setattr(dataset, "query", fail_daily)
    result = billing.get_daily_costs(days=1)
    assert result.data_quality.status == "unavailable"
    assert result.days[0].net_cost is None and result.previous_total_net_cost is None


def test_estimated_build_ledger_is_separate_at_bogota_month_boundary(
    dataset, monkeypatch
):
    instant = datetime(2026, 10, 1, 4, 59, tzinfo=UTC)
    monkeypatch.setattr(billing, "utc_now", lambda: instant)
    monkeypatch.setattr(costs.config.cloud_build, "usage_enabled", True)
    months = []
    monkeypatch.setattr(
        costs.cloud_build_usage,
        "summary",
        lambda month: (
            months.append(month) or {"month": month, "estimated_cost_usd": 1000}
        ),
    )
    result = costs.get_cost_summary(days=1, month_to_date=True)
    assert result.period.start == "2026-09-01"
    assert result.cloud_build.month == "2026-10" and months == ["2026-10"]
    assert result.cloud_build.estimated_cost_usd == 1000
    assert not result.estimates_included_in_total
    assert result.total_cost is None  # export rows arrive later than this snapshot


def test_missing_sku_cannot_be_hidden_by_another_component(dataset):
    from eng_platform_api.services.cost_alerts import _text

    dataset.rows = (
        records("2026-10-01", 4)
        + records("2026-09-30", 4)
        + records("2026-09-30", 4, sku="storage")
    )
    result = billing.get_cost_comparison()
    assert result.current.total_net_cost < result.previous.total_net_cost
    assert result.items[0].reason == "missing_or_changed_component_coverage"
    assert not result.comparable and result.net_change is None
    assert _text(result, NOW) is None


def test_gap_in_component_cannot_be_hidden_by_full_compute_hours(dataset):
    dataset.rows = records("2026-10-01", 4) + records("2026-09-30", 4)
    dataset.rows += records("2026-10-01", 3, sku="storage") + records(
        "2026-09-30", 4, sku="storage"
    )
    result = billing.get_cost_comparison()
    assert result.items[0].reason == "incomplete_component_coverage"
    assert not result.comparable and result.net_change is None


def test_service_aggregation_cannot_hide_missing_resource_same_sku(dataset):
    dataset.rows = (
        records("2026-10-01", 4)
        + records("2026-09-30", 4)
        + records("2026-09-30", 4, resource="other-db")
    )
    result = billing.get_cost_comparison(group_by="service")
    assert len(result.items) == 1
    assert result.items[0].reason == "missing_or_changed_component_coverage"
    assert not result.comparable and result.net_change is None


def test_daily_previous_total_requires_full_equal_observed_coverage(dataset):
    dataset.rows = records("2026-10-01", 4) + records("2026-09-30", 7)
    result = billing.get_daily_costs(days=1)
    assert result.days[0].net_cost == 3
    assert result.previous_total_net_cost is None and not result.previous_comparable
    assert billing.get_cost_comparison().net_change == 0
    dataset.rows = records("2026-10-01", 7) + records("2026-09-30", 7)
    result = billing.get_daily_costs(days=1)
    assert result.previous_comparable and result.previous_total_net_cost == 5.25
    assert result.days[0].net_cost == result.previous_total_net_cost
