"""Tests for Cloud Run metrics endpoints and caching."""

from unittest import mock

from fastapi.testclient import TestClient

from eng_platform_api.main import app
from eng_platform_api.models import CloudRunServiceMetrics, MetricsSummary
from eng_platform_api.services import gcp_monitoring

client = TestClient(app)


def test_metrics_endpoint_returns_summary_from_worker_thread():
    summary = MetricsSummary(
        period="last_24h",
        services=[CloudRunServiceMetrics(service_name="test-api")],
    )
    with mock.patch.object(gcp_monitoring, "get_metrics_summary", return_value=summary):
        response = client.get("/api/metrics/cloud-run/summary")

    assert response.status_code == 200
    assert response.json()["services"][0]["service_name"] == "test-api"


def test_metrics_summary_uses_short_cache():
    summary = MetricsSummary(period="last_24h", services=[])
    gcp_monitoring._metrics_cache = (gcp_monitoring.monotonic(), summary)
    assert gcp_monitoring.get_metrics_summary() is summary
