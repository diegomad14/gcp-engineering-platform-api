# ADR 0006 - BigQuery Billing Export as FinOps Data Source

## Status

Accepted (updated by the service-first v0.4.0 contract)

## Context

Engineering teams need cost visibility per service and environment. GCP Cloud Billing Export to BigQuery provides detailed usage cost data including resource-level labels. The platform should provide reusable SQL queries and a Python module to surface cost data — without requiring teams to write their own BigQuery queries.

## Decision

1. **BigQuery as read-only cost source** — Use Cloud Billing Export's `gcp_billing_export_resource_v1_*` table.
2. **Labels-based attribution** — Require `service`, `env`, `owner`, `cost_center` labels on Cloud Run services for cost grouping.
3. **SQL templates** — Provide parametrized queries for: cost by project, cost by service, cost by Cloud Run service, daily trend, top SKUs, unlabeled costs.
4. **Python module** — `gcp_billing_bigquery.py` builds parametrized queries with mock fallback.
5. **Platform API endpoint** — `GET /api/costs/summary` and `/by-service` serve cost data to the UI.

## Prerequisites (Manual, Not Automated)

- Cloud Billing Export to BigQuery must be enabled by project owner in GCP Console.
- BigQuery dataset must exist.
- Platform runtime service account needs `roles/bigquery.dataViewer` on the billing dataset.
- None of these are automated by the platform in MVP.

## Label Strategy

```yaml
labels:
  service: cgm-sanplat-api
  env: prod
  owner: cgm
  cost_center: cgm
```

Applied to Cloud Run services via `--labels` flag or service YAML.

## Consequences

- Cost data becomes queryable from the platform API and UI.
- Label hygiene becomes critical — unlabeled resources appear in "unlabeled costs" queries.
- BigQuery query costs apply; aggregate views should be created for production dashboards.
- Billing export latency (up to 24h) means costs are near-real-time, not real-time.
