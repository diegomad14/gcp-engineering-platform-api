"""Tests for BigQuery billing cost queries."""

from datetime import date

from fastapi.testclient import TestClient

from eng_platform_api.main import app
from eng_platform_api.models import CostPeriod
from eng_platform_api.services.gcp_billing_bigquery import (
    _build_items_sql,
    _split_daily_rows,
    build_cost_query,
)

client = TestClient(app)


def test_cost_summary_endpoint():
    response = client.get("/api/costs/summary")
    assert response.status_code == 200
    data = response.json()
    assert data["currency"] == "USD"
    assert "period" in data
    assert "items" in data


def test_cost_summary_month_to_date():
    response = client.get("/api/costs/summary?month_to_date=true")
    assert response.status_code == 200


def test_cost_by_service_endpoint():
    assert client.get("/api/costs/by-service").status_code == 200


def test_cost_by_app_endpoint_removed():
    assert client.get("/api/costs/by-app").status_code == 404


def test_build_cost_query_project_level():
    query = build_cost_query(group_by="project")
    assert "SUM(cost)" in query
    assert "GROUP BY" in query
    assert "_PARTITIONTIME" in query
    assert "UNNEST(labels)" not in query


def test_build_cost_query_by_service():
    query = build_cost_query(group_by="service")
    assert "service.description" in query
    assert "resource.name" in query
    assert " app" not in query.lower()


def test_build_cost_query_by_sku():
    query = build_cost_query(group_by="sku")
    assert "sku.id" in query
    assert "sku.description" in query


def test_cost_by_sku_endpoint():
    response = client.get("/api/costs/by-sku")
    assert response.status_code == 200
    assert response.json()["currency"] == "USD"


def test_daily_costs_endpoint():
    response = client.get("/api/costs/daily")
    assert response.status_code == 200
    data = response.json()
    assert data["currency"] == "USD"
    assert isinstance(data["days"], list)
    assert "previous_period" in data
    assert "previous_total_net_cost" in data


def test_daily_costs_month_to_date():
    assert client.get("/api/costs/daily?month_to_date=true").status_code == 200


def test_items_sql_by_resource():
    query = _build_items_sql("t", "cost_type = 'regular'", group_by="resource")
    assert "resource.name" in query
    assert "GROUP BY project_id, gcp_service, service_name" in query


def test_items_sql_by_service():
    query = _build_items_sql("t", "cost_type = 'regular'", group_by="service")
    assert "resource.name" not in query
    assert "sku.description" not in query
    assert "GROUP BY project_id, gcp_service" in query


def test_items_sql_by_sku():
    query = _build_items_sql("t", "cost_type = 'regular'", group_by="sku")
    assert "sku.description" in query
    assert "resource.name" not in query


def test_split_daily_rows_fills_gaps_and_splits_windows():
    current = CostPeriod(start="2026-07-01", end="2026-07-05")
    previous = CostPeriod(start="2026-06-01", end="2026-06-30")
    rows = [
        (date(2026, 6, 15), 2.0, -0.5),  # previous window
        (date(2026, 7, 1), 1.0, -0.25),
        (date(2026, 7, 3), 3.0, 0.0),
        (date(2026, 8, 1), 99.0, 0.0),  # outside both windows: dropped
    ]

    series, previous_total = _split_daily_rows(rows, current, previous)

    assert [d.date for d in series] == [
        "2026-07-01",
        "2026-07-02",
        "2026-07-03",
        "2026-07-04",
        "2026-07-05",
    ]
    assert series[0].net_cost == 0.75
    assert series[1].net_cost == 0.0  # gap filled with zeros
    assert series[2].net_cost == 3.0
    assert previous_total == 1.5
