"""Tests for Cloud Run metrics endpoints and caching."""

from unittest import mock

from fastapi.testclient import TestClient

from eng_platform_api.main import app
from eng_platform_api.models import (
    CatalogResponse,
    CatalogService,
    CloudRunServiceMetrics,
    MetricsSummary,
)
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


def test_metrics_for_service_aggregates_monitoring_values():
    with (
        mock.patch.object(gcp_monitoring.monitoring_v3, "MetricServiceClient"),
        mock.patch.object(
            gcp_monitoring, "_get_metric_value", side_effect=[10, 20, 0.3, 0.4, 2]
        ),
        mock.patch.object(gcp_monitoring, "_get_error_rate", return_value=1.5),
    ):
        metrics = gcp_monitoring._get_metrics_for_service("project", "test-api")

    assert metrics == CloudRunServiceMetrics(
        service_name="test-api",
        request_count=10,
        error_rate=1.5,
        p95_latency_ms=20,
        cpu_utilization=0.3,
        memory_utilization=0.4,
        instances_max=2,
    )


def test_metrics_for_services_queries_services_concurrently():
    with (
        mock.patch.object(gcp_monitoring.config, "mock_mode", False),
        mock.patch.object(
            gcp_monitoring,
            "_get_metrics_for_service",
            side_effect=lambda project, service: CloudRunServiceMetrics(
                service_name=service
            ),
        ) as get_service_metrics,
    ):
        metrics = gcp_monitoring.get_metrics_for_services(["one", "two"])

    assert [metric.service_name for metric in metrics] == ["one", "two"]
    assert get_service_metrics.call_count == 2


def test_metrics_summary_refreshes_expired_cache():
    service = CatalogService(
        service_name="test-api",
        repository="test-org/test-api",
        owner="platform",
        project_id="project",
        region="us-central1",
    )
    expected = [CloudRunServiceMetrics(service_name="test-api")]
    gcp_monitoring._metrics_cache = None
    with (
        mock.patch.object(gcp_monitoring.config, "mock_mode", True),
        mock.patch(
            "eng_platform_api.services.catalog.get_services",
            return_value=CatalogResponse(services=[service], total=1),
        ),
        mock.patch.object(
            gcp_monitoring, "get_metrics_for_services", return_value=expected
        ) as get_metrics,
    ):
        summary = gcp_monitoring.get_metrics_summary()

    assert summary.services == expected
    get_metrics.assert_called_once_with(["test-api"])


def test_metrics_summary_uses_short_cache():
    summary = MetricsSummary(period="last_24h", services=[])
    gcp_monitoring._metrics_cache = (gcp_monitoring.monotonic(), summary)
    assert gcp_monitoring.get_metrics_summary() is summary
