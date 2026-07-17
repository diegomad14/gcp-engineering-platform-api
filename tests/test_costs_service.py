"""Tests for the billing service's BigQuery paths, using a fake client."""

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from eng_platform_api.services import gcp_billing_bigquery as billing


class FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class FakeClient:
    """Minimal stand-in for google.cloud.bigquery.Client."""

    def __init__(
        self, project=None, items_rows=None, totals_rows=None, daily_rows=None
    ):
        self.items_rows = items_rows or []
        self.totals_rows = totals_rows or []
        self.daily_rows = daily_rows or []
        self.list_tables_calls = 0

    def dataset(self, name):
        return name

    def list_tables(self, dataset_ref, max_results=10):
        self.list_tables_calls += 1
        return [SimpleNamespace(table_id="gcp_billing_export_resource_v1_TEST")]

    def query(self, sql):
        if "usage_date" in sql:
            return FakeJob(self.daily_rows)
        if "total_cost" in sql or "COUNT(*)" in sql:
            return FakeJob(self.totals_rows)
        return FakeJob(self.items_rows)


@pytest.fixture
def real_billing(monkeypatch):
    """Force real (non-mock) mode with a fake BigQuery client installed."""
    fake = FakeClient(
        items_rows=[
            SimpleNamespace(
                project_id="p1",
                service_name="projects/1/instances/vm",
                gcp_service="Compute Engine",
                cost=3.5,
                credits=-0.5,
                net_cost=3.0,
            ),
        ],
        totals_rows=[SimpleNamespace(total_cost=10.0, total_credits=-2.0, n=42)],
        daily_rows=[
            # first day of the current month and mid previous month, so the
            # windows are stable regardless of when the test runs
            SimpleNamespace(
                usage_date=date.today().replace(day=1), cost=1.0, credits=-0.25
            ),
            SimpleNamespace(
                usage_date=(date.today().replace(day=1) - timedelta(days=1)).replace(
                    day=15
                ),
                cost=4.0,
                credits=-1.0,
            ),
        ],
    )
    monkeypatch.setattr(billing.config, "mock_mode", False)
    monkeypatch.setattr(billing, "_table_cache", None)
    monkeypatch.setattr(billing.bigquery, "Client", lambda project=None: fake)
    return fake


def test_query_billing_returns_items_and_totals(real_billing):
    summary = billing.get_cost_summary(days=30)
    assert summary.total_cost == 10.0
    assert summary.total_net_cost == 8.0
    assert len(summary.items) == 1
    assert summary.items[0].service_name == "projects/1/instances/vm"
    assert summary.items[0].net_cost == 3.0


def test_cost_summary_month_to_date_real_path(real_billing):
    summary = billing.get_cost_summary(month_to_date=True)
    assert summary.period.start.endswith("-01")
    assert summary.total_credits == -2.0


def test_billing_table_lookup_is_memoized(real_billing):
    billing.get_cost_summary(days=7)
    billing.get_cost_summary(days=7)
    assert real_billing.list_tables_calls == 1


def test_billing_status_real_path(real_billing):
    status = billing.get_billing_status()
    assert status["billing_export_enabled"] is True
    assert status["row_count"] == 42
    assert "Real cost data" in status["message"]


def test_daily_costs_real_path_fills_gaps(real_billing):
    series = billing.get_daily_costs(days=30, month_to_date=True)
    by_date = {d.date: d for d in series.days}
    today = date.today()
    first = today.replace(day=1).isoformat()
    assert series.period.start == first
    # every day of the window is present, gaps filled with zeros
    assert len(series.days) == today.day
    assert by_date[first].net_cost == 0.75
    if today.day >= 2:
        assert by_date[today.replace(day=2).isoformat()].net_cost == 0.0
    # mid-previous-month row lands in the previous window
    assert series.previous_total_net_cost == 3.0


def test_daily_costs_rolling_window(real_billing):
    series = billing.get_daily_costs(days=7)
    assert len(series.days) == 8  # start..today inclusive
    assert series.previous_period.end < series.period.start


def test_query_billing_error_returns_empty(monkeypatch):
    monkeypatch.setattr(billing.config, "mock_mode", False)
    monkeypatch.setattr(billing, "_table_cache", None)

    class BrokenClient:
        def __init__(self, project=None):
            pass

        def dataset(self, name):
            return name

        def list_tables(self, dataset_ref, max_results=10):
            return [SimpleNamespace(table_id="gcp_billing_export_resource_v1_TEST")]

        def query(self, sql):
            raise RuntimeError("boom")

    monkeypatch.setattr(billing.bigquery, "Client", lambda project=None: BrokenClient())
    summary = billing.get_cost_summary(days=30)
    assert summary.items == []
    assert summary.total_cost == 0.0
    series = billing.get_daily_costs(days=7)
    assert all(d.net_cost == 0.0 for d in series.days)
