"""Cloud Monitoring service — real metrics for Cloud Run services.

Queries Cloud Monitoring API for operational metrics.
All Cloud Run services have metrics available automatically.
"""

from datetime import datetime, timedelta, timezone

from google.cloud import monitoring_v3

from ..config import config
from ..models import CloudRunServiceMetrics, MetricsSummary

_METRIC_REQUEST_COUNT = "run.googleapis.com/request_count"
_METRIC_LATENCY = "run.googleapis.com/request_latencies"
_METRIC_CPU = "run.googleapis.com/container/cpu/utilizations"
_METRIC_MEMORY = "run.googleapis.com/container/memory/utilizations"
_METRIC_INSTANCE_COUNT = "run.googleapis.com/container/instance_count"


def _get_metric_value(
    client: monitoring_v3.MetricServiceClient,
    project_id: str,
    service_name: str,
    metric_type: str,
    minutes: int = 1440,
    per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_MEAN,
    cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_MEAN,
) -> float:
    """Get an aggregated value for a Cloud Run metric."""
    now = datetime.now(timezone.utc)
    interval = monitoring_v3.TimeInterval(
        end_time={"seconds": int(now.timestamp()), "nanos": 0},
        start_time={
            "seconds": int((now - timedelta(minutes=minutes)).timestamp()),
            "nanos": 0,
        },
    )
    aggregation = monitoring_v3.Aggregation(
        alignment_period={"seconds": minutes * 60},
        per_series_aligner=per_series_aligner,
        cross_series_reducer=cross_series_reducer,
    )

    filter_str = (
        f'metric.type="{metric_type}" '
        'resource.type="cloud_run_revision" '
        f'resource.labels.service_name="{service_name}"'
    )

    try:
        results = client.list_time_series(
            request={
                "name": f"projects/{project_id}",
                "filter": filter_str,
                "interval": interval,
                "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                "aggregation": aggregation,
            }
        )
        for ts in results:
            if not ts.points:
                continue
            point = ts.points[0]
            val = (
                point.value.double_value
                or float(point.value.int64_value)
                or float(point.value.distribution_value.mean)
            )
            return round(val, 4)
    except Exception:
        pass

    return 0.0


def _get_error_rate(
    client: monitoring_v3.MetricServiceClient,
    project_id: str,
    service_name: str,
    minutes: int = 1440,
) -> float:
    """Calculate error rate from request count by response code class."""
    now = datetime.now(timezone.utc)
    interval = monitoring_v3.TimeInterval(
        end_time={"seconds": int(now.timestamp()), "nanos": 0},
        start_time={
            "seconds": int((now - timedelta(minutes=minutes)).timestamp()),
            "nanos": 0,
        },
    )

    total = 0.0
    errors = 0.0

    aggregation = monitoring_v3.Aggregation(
        alignment_period={"seconds": minutes * 60},
        per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
        cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
    )

    for code_class in ["5xx", "2xx", "4xx"]:
        filter_str = (
            f'metric.type="run.googleapis.com/request_count" '
            'resource.type="cloud_run_revision" '
            f'resource.labels.service_name="{service_name}" '
            f'metric.labels.response_code_class="{code_class}"'
        )
        try:
            results = client.list_time_series(
                request={
                    "name": f"projects/{project_id}",
                    "filter": filter_str,
                    "interval": interval,
                    "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                    "aggregation": aggregation,
                }
            )
            for ts in results:
                if not ts.points:
                    continue
                val = ts.points[0].value.int64_value
                if code_class == "5xx":
                    errors += val
                total += val
        except Exception:
            pass

    if total > 0:
        return round((errors / total) * 100, 2)
    return 0.0


def get_metrics_for_services(service_names: list[str]) -> list[CloudRunServiceMetrics]:
    """Query real Cloud Monitoring metrics for multiple services."""
    if config.mock_mode:
        return _mock_metrics_for(service_names)

    try:
        client = monitoring_v3.MetricServiceClient()
        project_id = config.monitoring.gcp_project_id or "cgm-assistant-prod"
        metrics_list = []

        for service_name in service_names:
            try:
                request_count = _get_metric_value(
                    client,
                    project_id,
                    service_name,
                    _METRIC_REQUEST_COUNT,
                    per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
                    cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
                )
                latency = _get_metric_value(
                    client,
                    project_id,
                    service_name,
                    _METRIC_LATENCY,
                    per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_95,
                    cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_PERCENTILE_95,
                )
                cpu = _get_metric_value(
                    client,
                    project_id,
                    service_name,
                    _METRIC_CPU,
                    per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_95,
                    cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_PERCENTILE_95,
                )
                memory = _get_metric_value(
                    client,
                    project_id,
                    service_name,
                    _METRIC_MEMORY,
                    per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_95,
                    cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_PERCENTILE_95,
                )
                instance_count = _get_metric_value(
                    client,
                    project_id,
                    service_name,
                    _METRIC_INSTANCE_COUNT,
                    minutes=60,
                    per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_MAX,
                    cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_MAX,
                )
                error_rate = _get_error_rate(client, project_id, service_name)

                metrics_list.append(
                    CloudRunServiceMetrics(
                        service_name=service_name,
                        request_count=int(request_count),
                        error_rate=error_rate,
                        p95_latency_ms=latency,
                        cpu_utilization=cpu,
                        memory_utilization=memory,
                        instances_max=int(instance_count),
                    )
                )
            except Exception:
                metrics_list.append(CloudRunServiceMetrics(service_name=service_name))

        return metrics_list if metrics_list else _mock_metrics_for(service_names)
    except Exception:
        return _mock_metrics_for(service_names)


def _mock_metrics_for(service_names: list[str]) -> list[CloudRunServiceMetrics]:
    """Fallback mock metrics when real API is unavailable."""
    return [CloudRunServiceMetrics(service_name=name) for name in service_names]


def get_metrics_summary() -> MetricsSummary:
    """Return metrics summary for Cloud Run services."""
    from .catalog import _list_cloud_run_services

    services = _list_cloud_run_services()
    service_names = [s.service_name for s in services]
    metrics = get_metrics_for_services(service_names)
    return MetricsSummary(period="last_24h", services=metrics)
