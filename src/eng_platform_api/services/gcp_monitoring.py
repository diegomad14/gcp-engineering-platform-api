"""Cloud Monitoring service — metrics for Cloud Run services.

MVP: Returns mock data. Real Cloud Monitoring integration requires:
- GCP project with Cloud Run services
- Platform SA with roles/monitoring.viewer
"""

from ..config import config
from ..models import CloudRunServiceMetrics, MetricsSummary


def _mock_metrics() -> list[CloudRunServiceMetrics]:
    """Realistic mock metrics for development and testing."""
    return [
        CloudRunServiceMetrics(
            service_name="cgm-sanplat-api",
            request_count=1200,
            error_rate=0.3,
            p95_latency_ms=420.0,
            cpu_utilization=0.31,
            memory_utilization=0.42,
            instances_max=2,
        ),
        CloudRunServiceMetrics(
            service_name="cgm-sanplat-web",
            request_count=800,
            error_rate=0.1,
            p95_latency_ms=150.0,
            cpu_utilization=0.15,
            memory_utilization=0.22,
            instances_max=1,
        ),
    ]


# Known Cloud Run metric types — for reference and future query building
CLOUD_RUN_METRICS = {
    "request_count": "run.googleapis.com/request_count",
    "request_latencies": "run.googleapis.com/request_latencies",
    "cpu_utilization": "run.googleapis.com/container/cpu/utilizations",
    "memory_utilization": "run.googleapis.com/container/memory/utilizations",
    "instance_count": "run.googleapis.com/container/instance_count",
    "billable_time": "run.googleapis.com/container/billable_instance_time",
    "startup_latencies": "run.googleapis.com/container/startup_latencies",
    "network_received": "run.googleapis.com/container/network/received_bytes_count",
    "network_sent": "run.googleapis.com/container/network/sent_bytes_count",
}


def build_metrics_filter(service_name: str, metric_type: str) -> str:
    """Build a Cloud Monitoring API filter string.

    The caller is responsible for executing the API call.
    This only builds the filter string.
    """
    return (
        f'metric.type="{metric_type}" '
        'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{service_name}"'
    )


def get_metrics_summary() -> MetricsSummary:
    """Return metrics summary. Uses mock data unless monitoring is configured."""
    if not config.monitoring.enabled or config.mock_mode:
        services = _mock_metrics()
    else:
        services = _mock_metrics()  # TODO: Execute real Monitoring API queries

    return MetricsSummary(period="last_24h", services=services)
