# BigQuery Billing Cost Model

## Overview

Cost visibility is achieved through GCP Cloud Billing Export to BigQuery. The platform provides SQL templates and a Python query builder module. Cost attribution relies on resource labels.

## Prerequisites (Manual Setup)

1. Enable Cloud Billing Export to BigQuery in GCP Console.
2. Create or identify the BigQuery dataset (e.g., `billing_export`).
3. The detailed usage table will be named `gcp_billing_export_resource_v1_<BILLING_ACCOUNT_ID>`.
4. Grant the platform runtime service account `roles/bigquery.dataViewer` on the dataset.

## Label Strategy

Every Cloud Run service must have these labels for cost attribution:

```yaml
labels:
  service: cgm-sanplat-api           # Cloud Run service identifier
  env: prod                          # Environment (prod, staging, dev)
  owner: cgm                         # Owning team
  cost_center: cgm                   # Cost center for chargeback
```

### Applying Labels to Cloud Run

```bash
gcloud run services update SERVICE_NAME \
  --update-labels=service=my-service,env=prod,owner=team,cost_center=cc
```

Or via `gcloud run deploy`:
```bash
gcloud run deploy SERVICE_NAME \
  --labels=service=my-service,env=prod,owner=team,cost_center=cc
```

## Configuration

```yaml
billing:
  enabled: true
  bigquery:
    project_id: cgm-assistant-prod
    dataset: billing_export
    detailed_usage_table: gcp_billing_export_resource_v1_XXXXXX
    location: US
  labels:
    env_label: env
    owner_label: owner
    cost_center_label: cost_center
```

Environment variables:
```
ENG_PLATFORM_BILLING_ENABLED=true
ENG_PLATFORM_BQ_PROJECT_ID=cgm-assistant-prod
ENG_PLATFORM_BQ_DATASET=billing_export
ENG_PLATFORM_BQ_TABLE=gcp_billing_export_resource_v1_XXXXXX
```

## Queries

The live queries are built in
`src/eng_platform_api/services/gcp_billing_bigquery.py`:

| Function | Purpose |
|----------|---------|
| `_build_items_sql(group_by="resource")` | Line items per resource (`/api/costs/summary`) |
| `_build_items_sql(group_by="service")` | Cost grouped by `service.description` (`/api/costs/by-service`) |
| `_build_items_sql(group_by="sku")` | Cost grouped by `sku.description` (`/api/costs/by-sku`) |
| `get_daily_costs()` | Daily net cost trend plus previous-window total (`/api/costs/daily`) |
| `build_cost_query()` | Parametrized template (project/service/sku), kept for reference |

**Follow-up — label attribution**: queries group by `resource.name` and
`service.description`, not by the labels above, because most resources do not
carry the labels yet. Once `service`/`env`/`owner`/`cost_center` are applied
consistently, add a labels grouping (`UNNEST(labels)`) with an "unlabeled"
bucket.

## Cost Calculation

- `cost` — Amount after negotiated discounts.
- `credits[].amount` — Credits applied (negative values).
- `net_cost = SUM(cost) + SUM(credits)` — Effective cost after credits.
- All values in billing account currency (USD for `cgm-assistant-prod`).

## API Response Shape

```json
{
  "currency": "USD",
  "period": {
    "start": "2026-07-01",
    "end": "2026-07-31"
  },
  "items": [
    {
      "project_id": "cgm-assistant-prod",
      "service_name": "cgm-sanplat-api",
      "gcp_service": "Cloud Run",
      "cost": 12.34,
      "credits": -1.20,
      "net_cost": 11.14
    }
  ]
}
```

## BigQuery Costs

- Storage: First 10 GiB/month free, then ~$0.02/GB/month.
- Queries: $6.25/TiB scanned (on-demand), first 1 TiB/month free.
- **Recommendation:** Use `_PARTITIONTIME` filters and create summary views to minimize scan costs.

## Unlabeled Cost Handling

The `unlabeled_costs.sql` query identifies costs that cannot be attributed to a service, owner, or cost center. This is a FinOps hygiene metric — the goal is to drive unlabeled costs toward zero.
