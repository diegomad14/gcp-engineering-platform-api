# Bogotá consumption comparisons and limited cost alerts

This change is prepared locally for review. It does not deploy an API, change Cloud SQL, enable notifications, grant OAuth scopes, configure secrets, or create a schedule.

## Billing contract

`/api/costs/summary`, `/by-service`, `/by-sku`, `/daily`, and `/comparison` use the existing detailed BigQuery billing export for `cgm-assistant-prod`. Consumption windows use `usage_start_time` and `usage_end_time` in `America/Bogota`, with an exclusive end. Neither ingestion partitions nor invoice month determine consumption dates. An as-of export timestamp bounds every query in a comparison. The rolling `days` count includes today; month-to-date begins at Bogotá midnight on the first day.

`/comparison` matches the prior window's elapsed time, limits both windows to their shared observed consumption watermark, and reports the exact four timestamps. For month-to-date, the prior month begins on its first day; shorter prior months cap both comparison windows. The summary still reports the full requested current period. Daily series retain the requested observed rows, but their prior total is unknown unless the full requested window has equivalent verified component coverage (`previous_comparable=false` otherwise). Only rows whose usage interval fits entirely within the requested window are included. Crossing intervals are excluded rather than prorated.

All exported figures remain provisional. There is no proof that all SKU rows or later corrections have arrived. A resource qualifies for an observed comparison only when it exists in both periods with the same currency, start/end coverage, and enough distinct consumption hours for every underlying resource/SKU component. Missing or changed component sets and unattributed components prevent comparison, even when another SKU fills every resource-level hour. Absent resources and hourly gaps do not become a fall to zero. The overall delta is unknown unless every observed item qualifies. Individual qualifying items remain available with their own coverage metadata.

Each response exposes export freshness, observed usage bounds, row count, availability, currency, credits, and `is_complete=false`. Query failure, no exported rows, or mixed currencies produces a null total with an explicit reason. A daily date without rows has null costs and `has_data=false`; an exported zero is retained. This changes previously numeric-only totals to nullable values: API consumers must display unknown data explicitly. `cost_type='regular'` means consumption charges, not a finalized invoice including taxes or adjustments.

Cloud Build's UTC monthly collector ledger and estimates remain separate. They are never added to exported totals. Clients should use the export, not estimates, to calculate billed changes. Items are no longer limited to 50 or filtered by a monetary threshold, so credits and unallocated resources can reconcile with totals.

## MCP interface

Existing `get_cost_summary` remains. New read tools are `get_daily_costs`, `get_cost_by_service`, `get_cost_by_sku`, `get_billing_status`, and `get_cost_comparison`. They require `eng-platform.read` and use the existing sanitized MCP audit. `get_cost_comparison` supports resource, GCP service, or SKU grouping and a 1–365-day window.

`send_cost_alert()` has no parameters. A caller cannot supply message text, a URL, a recipient, a channel, or credentials. The endpoint requires the existing read scope plus the new explicit `eng-platform.cost-alerts.send` scope. Default OAuth registration remains read-only; existing tokens do not acquire the new scope. The MCP operator allowlist and a separate notification allowlist must authorize the same GitHub identity.

The only supported owner is `diegomad14`, and the only recipient identity is `76bfd5f9-0a15-4cc1-87b8-ad1b74a19165`. The supplied recipient was enabled for Telegram with an address ending in 7589. Those facts do **not** establish ownership of a private Telegram chat. Activation requires separate verification and an explicit server attestation of that association. The address must be a positive private-chat identifier; group identifiers are rejected. Its complete value never appears in tool arguments, responses, or audit payloads.

The server generates plain text from qualifying credit-adjusted observed resource changes of at least 0.10 currency units, with at most ten changes. Both export timestamps must be at most 48 hours old. Missing, invalid, stale, future-dated, mixed-currency, or uncomparable data is suppressed. Days without a qualifying change return `no_comparable_change` without sending. The alert always describes the figures as provisional and excludes build estimates.

## Delivery and activation requirements

The fixed communications gateway is `https://communications-ms-pzzhmu7una-uc.a.run.app/api/v2/messages`. Its contract was checked against communications-ms commit `3cbc86a8efc04888fc3dca96e286c0fad75e2835`: bearer service authentication, `messages:send`, `Idempotency-Key`, `recipient.address`, plain text, and HTTP 202 with `message_id`/`status`. The gateway does not resolve the Artemis recipient UUID into an address. Eng-platform's OAuth access token cannot replace a communications credential, and eng-platform does not retrieve a Telegram bot token.

Activation, after separate review and successful release checks, would require:

1. Verified owner/private-chat association and approved server configuration for the exact recipient.
2. An existing authorized communications credential supplied securely to the API runtime, with send authorization. No credential is created, read from Secret Manager, or copied by this change.
3. Explicit configuration of `ENG_PLATFORM_COST_ALERTS_ENABLED`, `ENG_PLATFORM_COST_ALERTS_ALLOWED_LOGINS`, `ENG_PLATFORM_COST_ALERTS_OWNER_LOGIN`, `ENG_PLATFORM_COST_ALERTS_RECIPIENT_ID`, `ENG_PLATFORM_COST_ALERTS_PRIVATE_DESTINATION_CONFIRMED`, `ENG_PLATFORM_COST_ALERTS_RECIPIENT_ADDRESS`, and `ENG_PLATFORM_COST_ALERTS_COMMUNICATIONS_API_KEY`. Defaults block sending; address and key are excluded from configuration repr.
4. A durable private MCP Firestore store and audit collection using the runtime's existing permissions, and a separately authorized OAuth session with the notification scope. Runtime permissions must be checked before activation; this change creates no IAM bindings or collections during preparation.

One notification is reserved per recipient per Bogotá day in the existing private MCP store. Firestore transactions arbitrate concurrent replicas. A 120-second lease protects dispatch, and a gateway idempotency key plus a frozen server payload survives an ambiguous transport result. A changed destination within the same alert day is rejected. Gateway acceptance is reported separately from provider acceptance; HTTP 202 never sets `delivery_confirmed=true`. A terminal/unknown provider status does not trigger a fresh message. The gateway owns its delivery retries. Internal alert records contain the generated cost text and should stay in the existing private OAuth/state collection; retention should be reviewed during activation.

## Practical daily review

No monitor or automation is active. Until this revision is deployed and configured, the new tools are unavailable in production. A manual daily review can use the existing billing export, inspect freshness first, then compare equal observed consumption windows and examine seven- and thirty-day trends alongside Cloud SQL utilization, connection pressure, storage, and errors. Do not reduce capacity based on a partial billing decrease alone.

A local scheduled workflow would require the Mac online and credentials available. A future cloud automation calling the deployed, authorized MCP would not depend on this Mac. Its connected app, refresh behavior, scopes, cadence, notification approval, and missing-data behavior need verification before creating it. This change prepares a callable tool, not a scheduler. Database or capacity changes must still be presented separately.

## Local validation limits

Validation reuses the existing Python 3.14 environment and also runs the full suite in an isolated Python 3.12 container. No dependencies were installed on the Mac; validation images contain their own dependencies. Tests use synthetic export rows and mocked notification transport/persistence. They cover Bogotá/UTC/Los Angeles month boundaries, equal watermarks, delayed ingestion, missing resources and hours, currency/credits, unknown errors, Cloud Build separation, private recipient authorization, scope checks, fresh export requirements, concurrency, retries, deduplication, and gateway/provider status distinctions.

Run the exact-commit normalized OSS gate before release, with policy `oss-v2`, global coverage at least 70%, and changed-line coverage at least 80%. The default sandbox cannot reach the Docker socket or resolve registry DNS. Approved executor access confirmed that the host daemon and DNS work, without restarting Docker, changing permissions, or disabling TLS. Full Semgrep and Trivy scans then completed. Fresh dependency audits cover the patched lock and the actual application image; the approved lock update changes only PyJWT to 2.15.0. Existing credentials also validated resource, service, and SKU GoogleSQL with BigQuery dry-runs, without executing billed queries or reading rows. Firestore transactions and gateway delivery remain tested with mocks. No push, remote merge, deploy, live send, or schedule has been performed. The isolated local branch was rebased onto current remote main `adf3a37d8b05eaa1ccfe2f5bfee0348904c83d72`: the authorized GitHub connector supplied the Git metadata and two changed files; their blob hashes, root tree hash and signed commit object hash were verified exactly before import. Artemis PR 137 is outside this change; its scopes/consent must be checked separately before activation.

## Paired frontend preparation

The isolated Engineering Platform Web change must ship before this API contract is promoted. It renders null totals as Unknown, preserves daily gaps, requires explicit comparable coverage for a delta, and suppresses forecasts for incomplete exports. Its generated schema follows this API. The cost panel refreshes hourly instead of every five minutes to limit repeated coverage queries; exported costs remain delayed and provisional. Compatible brace-expansion lock updates remove high-severity advisories. Moderate Vitest tooling advisories remain documented at the unchanged repository security threshold. Both repositories retain exact-commit gate evidence; neither has been pushed, merged, or deployed.
