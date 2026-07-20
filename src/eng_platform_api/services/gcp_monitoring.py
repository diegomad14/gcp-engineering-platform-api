"""Cloud Monitoring service — real metrics for Cloud Run services.

Queries Cloud Monitoring API for operational metrics.
All Cloud Run services have metrics available automatically.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import partial
from threading import Lock
from time import monotonic

from google.cloud import monitoring_v3

from ..config import config
from ..models import CloudRunServiceMetrics, MetricsSummary

_METRIC_REQUEST_COUNT = "run.googleapis.com/request_count"
_METRIC_LATENCY = "run.googleapis.com/request_latencies"
_METRIC_CPU = "run.googleapis.com/container/cpu/utilizations"
_METRIC_MEMORY = "run.googleapis.com/container/memory/utilizations"
_METRIC_INSTANCE_COUNT = "run.googleapis.com/container/instance_count"
_METRICS_CACHE_TTL_SECONDS = 60
_metrics_cache: dict[int, tuple[float, MetricsSummary]] = {}
_metrics_cache_lock = Lock()


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


def _get_metrics_for_service(
    project_id: str, service_name: str, minutes: int = 1440
) -> CloudRunServiceMetrics:
    try:
        client = monitoring_v3.MetricServiceClient()
        return CloudRunServiceMetrics(
            service_name=service_name,
            request_count=int(
                _get_metric_value(
                    client,
                    project_id,
                    service_name,
                    _METRIC_REQUEST_COUNT,
                    minutes=minutes,
                    per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_SUM,
                    cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
                )
            ),
            error_rate=_get_error_rate(
                client, project_id, service_name, minutes=minutes
            ),
            p95_latency_ms=_get_metric_value(
                client,
                project_id,
                service_name,
                _METRIC_LATENCY,
                minutes=minutes,
                per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_95,
                cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_PERCENTILE_95,
            ),
            cpu_utilization=_get_metric_value(
                client,
                project_id,
                service_name,
                _METRIC_CPU,
                minutes=minutes,
                per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_95,
                cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_PERCENTILE_95,
            ),
            memory_utilization=_get_metric_value(
                client,
                project_id,
                service_name,
                _METRIC_MEMORY,
                minutes=minutes,
                per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_95,
                cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_PERCENTILE_95,
            ),
            instances_max=int(
                _get_metric_value(
                    client,
                    project_id,
                    service_name,
                    _METRIC_INSTANCE_COUNT,
                    minutes=60,
                    per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_MAX,
                    cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_MAX,
                )
            ),
        )
    except Exception:
        return CloudRunServiceMetrics(service_name=service_name)


def get_metrics_for_services(
    service_names: list[str], minutes: int = 1440
) -> list[CloudRunServiceMetrics]:
    """Query Cloud Monitoring concurrently for multiple services."""
    if config.mock_mode:
        return _mock_metrics_for(service_names)

    project_id = config.monitoring.gcp_project_id or "cgm-assistant-prod"
    worker_count = min(6, max(1, len(service_names)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        get_service_metrics = partial(
            _get_metrics_for_service, project_id, minutes=minutes
        )
        return list(executor.map(get_service_metrics, service_names))


def _mock_metrics_for(service_names: list[str]) -> list[CloudRunServiceMetrics]:
    """Fallback mock metrics when real API is unavailable."""
    return [CloudRunServiceMetrics(service_name=name) for name in service_names]


def get_metrics_summary(minutes: int = 1440) -> MetricsSummary:
    """Return metrics summary for Cloud Run services."""
    from .catalog import get_services

    global _metrics_cache
    with _metrics_cache_lock:
        now = monotonic()
        cached = _metrics_cache.get(minutes)
        if cached and now - cached[0] < _METRICS_CACHE_TTL_SECONDS:
            return cached[1]
        services = get_services().services
        service_names = [s.service_name for s in services]
        summary = MetricsSummary(
            period="1h" if minutes == 60 else "24h",
            services=get_metrics_for_services(service_names, minutes=minutes),
        )
        _metrics_cache[minutes] = (monotonic(), summary)
        return summary
