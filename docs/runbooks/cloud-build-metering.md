# Cloud Build policy and reconcilable consumption

All maintained new-submit paths validate the same `cloud_build_policy` contract:
omit `machineType`, pool and extra disk; use `CLOUD_LOGGING_ONLY` and the existing
profile timeout (at most 3,600 seconds). No request or environment override is
accepted. Deploy, rollback, quality, planner/retry and the fallback controller
enforce it before submission. Tooling publication uses GitHub Docker, not Cloud
Build. CI checks active generators/workflows for reintroduced HIGHCPU settings.
Historical build inspection remains permitted. An administrator can still submit
outside these paths; inventory counts those builds as external and flags policy
deviations. No automatic spending stop is added.

## Independent ledger

The existing authenticated release-reconcile Scheduler also invokes a separately
throttled 15-minute collector. `cloud_build_usage` is private, with one document
per project/region/build ID and transactional upserts. It inventories us-central1
and global with pagination/checkpoints, a 24-hour overlap, and pending-build polls.
Failures, cancellations and timeouts count; no-start terminal builds cost zero
runtime minutes. Missing finish timestamps stay pending. Each attempt's duration
is allocated by intersection with UTC calendar months, never the retry month.

Classification requires a bound execution ID/build ID/fingerprint/repo/service/SHA,
not a tag. Functional execution state, releases and traffic are never modified by
metering. Builds whose identity cannot be verified remain `other`. Historical
execution documents and timing fields are preserved, but are not the new total.

`release_minutes + deployment_minutes = total_minutes` (platform).
`total_minutes + other_minutes = project_minutes` (all project builds).
Estimated gross compute is separate from Billing gross, credits and net. Billing
uses the same explicit UTC usage month, cached transactionally at most hourly.
`updated_at`, `billing_updated_at`, `billing_exported_at` and availability flags
identify freshness. Export lag and provider metering can differ from runtime.
No unavailable value is fabricated as zero. REST/MCP use the same snapshot.

Alerts at 2,000/2,250/2,500 minutes apply to the entire project, including external
builds. They are operational thresholds, not a promise of remaining free quota.
List-rate estimates for historical machine types remain available for audits.

## Rollout and backfill

1. Publish API via the canonical release process. Initially keep
   `ENG_PLATFORM_CLOUD_BUILD_USAGE_ENABLED=false` (default): collection runs but
   the public `cloud_build` field is null.
2. With ADC and existing Cloud Build read / Firestore permissions, inspect:

   ```bash
   python scripts/ops/cloud_build_usage_backfill.py --project cgm-assistant-prod --month 2026-09
   ```

   Dry-run is the default: no ledger, billing-cache or functional-state writes.
   Match the audited build IDs: 362.30 platform minutes (341.68 release/quality,
   20.62 deployment), 300.73 other, 663.03 project at the original cutoff. New
   builds legitimately increase these totals; reconcile by ID, not blind totals.
3. Repeat with `--apply`. Only the ledger/snapshots/cache are written. Re-running
   is idempotent. Confirm no unresolved identity or unexpected duplicate attempt.
4. Enable `ENG_PLATFORM_CLOUD_BUILD_USAGE_ENABLED=true`, then release the web.
   If the counter malfunctions disable only this flag: keep collection/audit and
   the default-machine enforcement in place.
5. Inspect the next necessary production build, not a synthetic canary: no
   explicit machine/pool/disk. Compare its eventual Billing SKU/cost to the
   export. Default-machine savings are not established until Billing confirms.

Tests use mocks only and must not submit Cloud Builds. Keep the historical Google
Billing anomaly open separately; this change does not settle previous charges.
