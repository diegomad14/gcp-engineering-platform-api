"""Tests for BigQuery billing cost queries."""

from fastapi.testclient import TestClient

from eng_platform_api.main import app
from eng_platform_api.services.gcp_billing_bigquery import build_cost_query

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
