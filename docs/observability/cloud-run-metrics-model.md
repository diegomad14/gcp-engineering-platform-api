# Cloud Run Metrics Model

## Overview

Cloud Run services automatically export metrics to Cloud Monitoring. The platform provides a Python module that defines metric queries and returns structured data — using mocks when GCP credentials are not configured.

## Metrics Collected

### Core Metrics

| Metric | Type String | Description |
|--------|-------------|-------------|
| Request count | `run.googleapis.com/request_count` | Requests reaching the revision |
| Request latency | `run.googleapis.com/request_latencies` | Distribution: p50, p95, p99 |
| CPU utilization | `run.googleapis.com/container/cpu/utilizations` | Container CPU utilization |
| Memory utilization | `run.googleapis.com/container/memory/utilizations` | Container memory utilization |
| Instance count | `run.googleapis.com/container/instance_count` | Container instances by state |
| Error rate | Derived from `request_count` with `response_code_class=5xx` | Percentage of 5xx responses |
| Billable time | `run.googleapis.com/container/billable_instance_time` | Billable instance time |
| Startup latency | `run.googleapis.com/container/startup_latencies` | Container startup time |

### Derived Metrics

| Metric | Calculation |
|--------|-------------|
| Error rate % | `(5xx_count / total_count) * 100` |
| p95 latency | From `request_latencies` distribution |
| Instance max | Max value of `instance_count` over period |

## Monitoring API Query Pattern

```python
# Conceptual — actual implementation uses google-cloud-monitoring SDK
filter_str = (
    'metric.type="run.googleapis.com/request_count" '
    'resource.type="cloud_run_revision" '
    f'resource.labels.service_name="{service_name}"'
)
```

## API Response Shape

```json
{
  "period": "last_24h",
  "services": [
    {
      "service_name": "cgm-sanplat-api",
      "request_count": 1200,
      "error_rate": 0.3,
      "p95_latency_ms": 420,
      "cpu_utilization": 0.31,
      "memory_utilization": 0.42,
      "instances_max": 2
    }
  ]
}
```

## Mock Data Strategy

When `ENG_PLATFORM_MONITORING_ENABLED=false` (default), the API returns realistic mock data from `static_examples/mock_metrics.json`. This allows UI development and testing without GCP credentials.

When enabled, the module queries Cloud Monitoring API using Workload Identity Federation credentials.

## Configuration

```
ENG_PLATFORM_MONITORING_ENABLED=true
ENG_PLATFORM_GCP_PROJECT_ID=cgm-assistant-prod
```

## Sampling & Latency

- Metrics sampled every 60 seconds by Cloud Monitoring.
- Visible within ~120 seconds.
- Platform API adds no additional latency beyond Monitoring API response time.

## Future: Alerts & Uptime

Phase 2 may add:
- Uptime check configuration for critical endpoints.
- Alert policy definitions as code.
- SLI/SLO tracking per service.
