# Official Documentation Research

## Cloud Billing Export to BigQuery

**Source:** https://cloud.google.com/billing/docs/how-to/export-data-bigquery-tables

### Schema (Detailed Usage Cost Data)

Table name: `gcp_billing_export_resource_v1_<BILLING_ACCOUNT_ID>`
Date-partitioned on `_PARTITIONTIME` (pseudo-column) or `export_time`.

**Key fields for cost attribution:**

| Field | Type | Description |
|-------|------|-------------|
| `billing_account_id` | STRING | Cloud Billing account ID |
| `cost` | FLOAT | Cost after negotiated discounts |
| `cost_at_list` | FLOAT | Cost at list price before discounts |
| `currency` | STRING | Billing currency (e.g., "USD") |
| `credits[].amount` | FLOAT | Credit amount (negative = reduces cost) |
| `credits[].type` | STRING | COMMITTED_USAGE_DISCOUNT, DISCOUNT, FREE_TIER, PROMOTION, etc. |
| `service.id` | STRING | GCP service ID |
| `service.description` | STRING | e.g., "Cloud Run", "Compute Engine" |
| `sku.id` | STRING | SKU ID |
| `sku.description` | STRING | Human-readable SKU description |
| `project.id` | STRING | GCP Project ID |
| `project.name` | STRING | Project display name |
| `project.labels[].key/value` | RECORD | Project-level labels |
| `labels[].key/value` | RECORD | Resource-level labels |
| `system_labels[].key/value` | RECORD | System-generated labels |
| `location.location` | STRING | Multi-region, region, zone, or "global" |
| `location.region` | STRING | e.g., "us-central1" |
| `resource.name` | STRING | Service-specific resource identifier |
| `resource.global_name` | STRING | Globally unique resource identifier |
| `usage.amount` | FLOAT | Usage quantity |
| `usage.unit` | STRING | e.g., "byte-seconds", "seconds" |
| `usage_start_time` | TIMESTAMP | Usage window start |
| `usage_end_time` | TIMESTAMP | Usage window end |
| `export_time` | TIMESTAMP | When the row was exported |
| `invoice.month` | STRING | Invoice month (YYYYMM) |
| `cost_type` | STRING | regular, tax, adjustment, rounding_error |
| `tags[].key/value` | RECORD | Resource manager tags (detailed export) |
| `price.effective_price` | NUMERIC | Effective price per unit |
| `adjustment_info.type` | STRING | USAGE_CORRECTION, SLA_VIOLATION, etc. |

### Latency & Frequency

- Data appears within hours of enablement; ongoing updates occur at regular intervals.
- No strict latency SLA — costs typically appear within a day, sometimes >24h.
- Self-serve accounts: charges may appear 5-15 minutes after usage.
- Multi-region datasets include retroactive data from start of previous month.
- Pricing data exported once per day, up to 48h latency.

### BigQuery Costs

- Storage: First 10 GiB/month free. Active: ~$0.02/GB/month. Long-term after 90 days: ~50% less.
- Queries: $6.25/TiB (on-demand, after first 1 TiB/month free).
- **Recommendation:** Create summary views/aggregated queries to minimize scan costs.

## Cloud Monitoring for Cloud Run

**Source:** https://cloud.google.com/run/docs/monitoring

### Available Metrics (cloud_run_revision)

| Metric Type | Description |
|-------------|-------------|
| `run.googleapis.com/request_count` | Requests reaching revision (excludes rejected) |
| `run.googleapis.com/request_latencies` | Request latency distribution (ms) |
| `run.googleapis.com/container/cpu/utilizations` | CPU utilization distribution |
| `run.googleapis.com/container/memory/utilizations` | Memory utilization distribution |
| `run.googleapis.com/container/instance_count` | Container instances by state |
| `run.googleapis.com/container/billable_instance_time` | Billable time |
| `run.googleapis.com/container/startup_latencies` | Startup latency |
| `run.googleapis.com/container/network/received_bytes_count` | Network bytes received |
| `run.googleapis.com/container/network/sent_bytes_count` | Network bytes sent |
| `run.googleapis.com/container/max_request_concurrencies` | Max concurrent requests |
| `run.googleapis.com/scaling/recommended_instances` | Recommended instances |

### Labels

`service_name`, `revision_name`, `configuration_name`, `response_code_class` (2xx/4xx/5xx), `response_code`, `instance_id`, `state`.

### Sampling

- Every 60 seconds.
- Visible within ~120 seconds.
- No setup required — automatic for Cloud Run services.

## GitHub Actions Reusable Workflows

**Source:** https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows

### Key Requirements

- File must be in `.github/workflows/` (no subdirectories).
- Trigger: `on: workflow_call:`.
- Called via: `uses: owner/repo/.github/workflows/file.yml@ref` at the job level.
- A job with `uses:` cannot also have `steps:`.
- Input types: `boolean`, `number`, `string`.
- Secrets passed via `secrets:` or `secrets: inherit`.
- Nesting limit: 10 levels total.
- Permissions can only be maintained or reduced through the chain.

### Version Pinning

| Ref | Safety |
|-----|--------|
| Commit SHA `@a1b2c3d4` | Best — immutable |
| Major tag `@v1` | Good — recommended for platform |
| Specific tag `@v1.2.3` | Moderate |
| Branch `@main` | Unsafe — dev only |

**Recommendation:** Pin to major version tags (`@v1`) for platform workflows. Document SHA pinning for production-critical consumers.

## SonarQube Cloud GitHub Actions

**Source:** https://docs.sonarsource.com/sonarqube-cloud/analyzing-source-code/ci-based-analysis/github-actions-for-sonarcloud/

### Setup

- Action: `SonarSource/sonarqube-scan-action@v5` (composite, not Docker).
- Requires `SONAR_TOKEN` secret.
- `fetch-depth: 0` required for accurate blame/analysis.
- Project configuration via `sonar-project.properties` or action args.

### Quality Gate

**Blocking mode:**
```
-Dsonar.qualitygate.wait=true
-Dsonar.qualitygate.timeout=300
```
Scanner blocks until quality gate completes. Non-zero exit on failure → job fails.

**Non-blocking mode (default):**
Omit `sonar.qualitygate.wait`. Rely on GitHub branch protection rules with "SonarCloud Code Analysis" status check.

### Default Sonar Way Quality Gate

| Condition | Threshold |
|-----------|-----------|
| Coverage on new code | >= 80% |
| Duplicated lines on new code | <= 3% |
| Maintainability rating | A |
| Reliability rating | A |
| Security rating | A |

## Implications for Architecture

1. **BigQuery billing export must be enabled manually** by project owner before cost queries work.
2. **Labels strategy is critical** — `labels.app`, `labels.env`, `labels.owner`, `labels.cost_center` enable cost attribution to services.
3. **Cloud Monitoring is automatic** — no setup needed for Cloud Run metrics.
4. **Reusable workflows** must live in `.github/workflows/` and use `workflow_call` — no subdirectories, no push/pull_request triggers.
5. **SonarQube** should start in non-blocking mode, then transition to blocking once baseline is established.
6. **BigQuery query costs** can be managed with summary views and partition filtering.
