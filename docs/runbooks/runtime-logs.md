# Bounded runtime log viewer (disabled by default)

The viewer uses the pinned official `google-cloud-logging==3.16.3` SDK. It does
not activate APIs, create a database or indexes, provision IAM, deploy, or change
logging retention. There is no mock production fallback or browser Google
credential. Checked-in examples are disabled with no readers. Operational
resource identities and reader policies belong in the separately reviewed private
runtime catalog, never in public example fixtures. The global feature defaults
to OFF and the global reader allowlist to empty. New resources remain disabled
until separately approved. A policy change does not verify or alter production
IAM, connectivity or runtime environment configuration.

## Authorization and activation prerequisites

A real GitHub OAuth callback writes a signed `github_auth_provider=github_oauth`
session marker. Old sessions must sign in again. Development/mock login and IAP
headers do not qualify. Reading logs never grants deploy permission.

Access requires the intersection of real OAuth, the global log-reader allowlist,
the resource's `logs.enabled=true`, and its private `logs.allowed_logins` list.
Global enablement also requires `ENG_PLATFORM_MOCK_MODE=false`, GitHub OAuth and
a session signing secret of at least 32 characters. Unknown resources return
404; known disabled resources, missing policies or readers return 403 before
cache, Firestore or Logging access. `/api/auth/me` exposes the global
`can_view_logs` capability; it does not authorize all catalog resources.

See [catalog fixtures and private activation](runtime-log-catalog.md) for public
synthetic examples and the private review process. Configure only explicitly
approved readers and resources. Enable the global feature only after verifying
runtime prerequisites and the safe rollout procedure below. The single shared
limit remains 12 upstream Logging queries per minute.

The runtime authority is the configured private catalog JSON loaded and fully
validated by `log_catalog.load_catalog`, including its SHA-256 integrity pin.
Public packaged examples are for disabled/offline use, never a fallback for an
unavailable configured private authority. Each resource has explicit project,
region and deployment runtime kind, with a globally unique service name for the
existing URL. Missing, corrupt or duplicate catalog data fails closed with a
sanitized 503, never an old or partial snapshot. Limits are 4 MiB and 2048
resources/projects. Onboarding YAML is a proposal, not another runtime authority.
The onboarding CLI creates disabled policies only. Public catalog metadata has
`logs.configured` (an explicit valid policy exists) and `logs.enabled` (the
resource flag). Private reader lists are never returned by the API.

Default/example configuration deliberately remains disabled; private resource
policies do not change these fail-closed global defaults:

```dotenv
ENG_PLATFORM_LOGS_ENABLED=false
ENG_PLATFORM_LOGS_ALLOWED_GITHUB_LOGINS=
ENG_PLATFORM_LOGS_QUOTA_PROJECT_ID=
ENG_PLATFORM_LOGS_BUDGET_PROJECT_ID=
ENG_PLATFORM_LOGS_BUDGET_COLLECTION=
ENG_PLATFORM_LOGS_LOOKBACK_MINUTES=15
ENG_PLATFORM_LOGS_PAGE_SIZE=1000
ENG_PLATFORM_LOGS_BUFFER_ENTRIES=2000
ENG_PLATFORM_LOGS_BUFFER_BYTES=2097152
ENG_PLATFORM_LOGS_REQUEST_TIMEOUT_SECONDS=20
ENG_PLATFORM_LOGS_RESERVE_TIMEOUT_SECONDS=4
ENG_PLATFORM_LOGS_RPC_TIMEOUT_SECONDS=10
ENG_PLATFORM_LOGS_FINISH_TIMEOUT_SECONDS=4
```

`QUOTA_PROJECT_ID` is mandatory when enabled and explicitly selects one shared
Logging quota attribution for **all source projects, replicas and revisions**.
The SDK obtains ADC with that quota project, verifies the credential's quota
project, and supplies it to the official gRPC transport. Client caching is keyed
by quota project. Each operation captures the quota project and Firestore
project/collection as one immutable scope; completion uses its original document
even if configuration changes while the call is running. A scope change before
dispatch, including during cold SDK/ADC setup, prevents the RPC. It fails closed if
attribution is unavailable. Source projects never choose additional budgets.
Changing this quota project is an operational migration, not a way to add quota.

An already available Firestore database in `BUDGET_PROJECT_ID` coordinates the
budget. Use `eng_platform_log_budget` or `eng_platform_log_budget_<suffix>`
consistently across every reader replica/revision. The existing repository
Firestore client is not evidence of production connectivity or permissions.
No infrastructure or permission prerequisite has been verified by this change.
Missing or ambiguous coordination means no Logging call.

Review existing runtime permissions without granting them in this change:

- `logging.logEntries.list` in each authorized source project; use narrowly
  scoped log-reader permissions. Private/data-access logs require additional
  permissions which this feature does not assume or request automatically.
- Existing permission to use the explicit quota project, where required by
  Google quota attribution. Failure does not fall back to a different project.
- Existing Firestore get/create/update access and version-preconditioned atomic
  writes in the configured database. The dedicated collection namespace is a
  code boundary, not an IAM boundary. Do not silently broaden IAM to enable it.

ADC remains server-side. Provider errors, credentials, raw payloads and filters
are not copied into application logs or sent to browsers. Explorer links contain
only validated catalog coordinates, never free text or log values.

## HTTP contract

`POST /api/catalog/services/{service_id}/logs` is read-only. The JSON body keeps
free-text filters out of browser URLs and normal nginx/uvicorn access logs. GET
is unsupported. Unknown fields and query parameters are rejected. Requests
require JSON, `X-Requested-With: EngineeringPlatform`, and an `Origin` exactly
matching `ENG_PLATFORM_FRONTEND_URL`.

```json
{"limit":200,"lookback_minutes":15,"severity":"DEFAULT","text":"","revision":null,"execution":null,"task_index":null}
```

`limit` is 1–200; lookback is 1–15 minutes and is clamped to the configured
maximum. Severity is a minimum from DEFAULT through EMERGENCY. Text is a
case-insensitive substring of at most 200 characters searched **after redaction**
against message/payload. Revision and execution are exact lower-case resource
IDs up to 128 characters; task index is 0–999999. Revision applies to services;
execution/task apply to jobs. These filters only inspect the sanitized cache.
They never alter the upstream resource query.

Response fields:

- `entries`: newest first; `{id,timestamp,severity,message,payload,revision,
  execution,task_index,trace,span_id}`. Payload can be any JSON value including
  null. Stable hashes deduplicate overlapping samples; timestamps are UTC with
  fixed microseconds. Labels are allowlisted identifiers. `trace` is either null
  or `projects/{authorized resource project}/traces/{32 lowercase hex}`;
  `span_id` is 16 lowercase hex and is only retained with a valid trace. Neither
  field accepts arbitrary URLs, project IDs or HTML.
- `resource_generation`: a 64-lowercase-hex HMAC cache token over resource
  identity, policy and redaction version, keyed with the existing session secret.
  It does not expose the private ACL digest. The browser must discard accumulated
  rows when it changes (including within an open detail view), and must not merge
  samples without a generation token.
- `status`: `fresh`, `stale`, `throttled`, `unavailable` or `disabled`. Fresh
  requires a successful sample of this exact local resource/policy no more than
  15 seconds old, without cache eviction. A successful empty sample is different
  from an unobserved/throttled empty cache.
- `truncated`: true if a next-page token, saturated page, redaction clipping,
  retention eviction or response limit may omit content. It is conservative
  across the cache lifetime. An empty page with a token is still truncated.
- `cache_evicted`: sticky while that cache exists; some retained history was
  removed for global entry/byte bounds. It implies truncation and prevents fresh
  status, even if a recent sample arrived. If metadata itself was LRU-evicted,
  the recreated cache has no sample until it independently succeeds.
- `observed_at`: latest successful coordination observation for this resource;
  `last_success_at`: its latest upstream sample; `cache_age_seconds`: its age.
  Null success/age means no local sample (including a restarted/evicted cache).
  Another resource's success never supplies these values or a watermark.
- `next_poll_seconds`: 5–60 seconds; honor it. `queue_position`: 1–4096 or null.
  Queued participants poll every 5–10 seconds to renew their waiting membership.
  `queue_wait_seconds`: a nonnegative **minimum estimated wait**, or null; it is
  not a guaranteed ETA or the polling interval. It assumes the current queue
  remains active; expiring waiters can reduce it. Failures, pending permits and
  head-of-queue polling can make the actual wait substantially longer.
  `overloaded` identifies queue or local-state capacity exhaustion.
- `deferral_reason`: optional/null when not deferred; `cadence` is the normal
  five-second admission interval, `queue` is an earlier waiting participant,
  `budget` means all 12 charged permits are occupied, `pending` is an unresolved
  permit already owned by this participant, and `overload` is finite queue/cache
  capacity. Neither cadence nor FIFO waiting means Cloud Logging exhausted its
  quota. Older replicas may omit this field.
- `window_start`: actual requested local history cutoff. `explorer_url`: fixed
  Google Cloud Console origin with only a catalog-built resource query.
- `limitations`: machine-readable replica-local, incomplete/late sampling and
  queue-estimate limitations.

All responses, including errors, use `Cache-Control: no-store`. Errors include
401 (no real OAuth session), 403 (reader/origin rejected), 404 (unknown resource),
415 (not JSON), 422 (invalid filters), 499 (initial disconnect), and sanitized 503
(catalog/deadline failure). Deadline failures set `Retry-After: 60`. Provider and
coordination failures use typed unavailable/stale responses without raw details.
The frontend schema derives from this application's OpenAPI contract.

## Sampling and isolation

Each demand-driven sample queries **only the requested authorized resource**.
Project, region, type and the correct service/job label come from the catalog.
The final filter is guarded at 20,000 characters both during construction and
immediately before the SDK call. There is one project resourceName, no global OR
query, no user-supplied project/filter and no manual list of supported services.
Catalogs with 500 or more resources therefore do not expand one Logging filter.

Initial history is at most 15 minutes. Subsequent samples overlap that resource's
last local success by two minutes. Each sample consumes exactly one first page;
there is no pagination, hidden retry, tail stream or background polling after
viewers stop. A noisy source can truncate its own sample, but cannot fill another
resource's upstream page. No continuation/lossless historical coverage is claimed.

The cache and watermark key contains project, region, runtime kind, service name
and policy fingerprint. Catalog/policy changes invalidate data before any return,
including throttle/error paths, and are checked before dispatch and publication
of an in-flight result. A route-provided authorization guard rechecks the signed
session, global reader/enablement/OAuth configuration and current resource ACL
after reservation and again after cold SDK setup immediately before dispatch. The route also revalidates the caller's global and resource
ACL after I/O. A revoked old sample cannot repopulate the new identity's cache.

## One global budget and bounded FIFO

All source projects share **12 permits**, with a minimum five-second dispatch
cadence, under the one explicitly configured quota project. The Firestore document
ID is `quota-` plus SHA256 of that quota project. Schema version 3 stores only
scope, timestamps, opaque participant/permit IDs and FIFO metadata. It stores no
log contents, source coordinates, page tokens, free text or reader identities.
Existing incompatible/legacy/corrupt records fail closed rather than resetting.

Each participant is an opaque hash of a process-incarnation ID and resource
identity/policy. Multiple tabs for the same replica/resource coalesce on one cache
lock and FIFO identity. Distinct replicas require distinct local samples. FIFO
admission grants a permit only when its head participant requests its own sample;
it never sends one replica to fill another replica's cache. Admitted participants
leave the queue and return at its tail for later demand. New participants cannot
skip existing ones. A participant with an unresolved permit cannot dispatch again.

The FIFO holds at most 4096 participants. Waiting participants refresh their
activity when they poll and expire after more than 35 seconds of inactivity:
the maximum 10-second queued poll advice plus 25 seconds for request/transport
latency. The exact 35-second boundary remains valid. Stopping a viewer can still
hold the head until this bound; the next successful coordination after expiry
removes it. Suspended viewers rejoin at the tail. Full queues report overload and make
no Logging call. Active queue positions and wait estimates are exposed honestly.
FIFO fairness is conditional on continued polling and successful coordination;
there is no wall-clock freshness SLA. At five seconds per slot, 500 independent
participants need at least roughly 41.7 minutes for one round, and 8 replicas
viewing all 500 create 4000 participants. Poll alignment and failures increase
that time. This cannot provide a complete 15-minute history for every resource.

**Pending permits never expire and are never refunded**, even for cancellation,
failed setup, a lost acknowledgement or a call not dispatched after revocation.
A confirmed completion remains charged for another 60 seconds using Firestore
time. Delayed calls, failures and restarts therefore cannot cause more than 12
actual first-page dispatches in any rolling 60 seconds. Only the synchronous
owner can finalize its participant/token after its call has returned. An
ambiguous reserve write is not retried or followed by a Logging call. A process
restart gets a new participant but does not remove old pending charges.

Each CAS phase uses the public Firestore read and version-preconditioned write
APIs, retry=None, at most two seconds per RPC reduced to remaining phase time,
and at most three retries for definite version conflicts. Unknown/timeout writes
are not retried. Logging GAPIC retry is None and gRPC transparent retry is disabled.
The generated Logging gRPC logger is disabled and filtered before client creation
because its debug interceptor can otherwise log unsanitized provider responses.

The ceiling is at most 720 Logging calls/hour **globally for this viewer**, not
per source project or replica. Other applications can independently consume the
same external quota. This is not zero-cost: admitted calls normally require
reserve/finalize reads and writes, and queued/denied polling can also write FIFO
heartbeats. The shorter 5–10-second queue polling increases coordination traffic
relative to the previous maximum 60-second advice. The Logging ceiling does not
imply a 24-write/minute Firestore ceiling.
Costs depend on demand, contention and current provider pricing; no fixed dollar
amount or production latency is asserted here.

## Safe rollout and pending-permit recovery

The 35-second liveness change preserves schema 3, its document identity, and every
permit's ownership/completion semantics. Existing schema-3 replicas can therefore
reserve and finish against the same record during a rolling deployment. Their
older 60-second poll advice can outlive the new waiting lease, so an active old
waiter may lose its FIFO place and rejoin at the tail; it never loses a pending
permit. Complete the rollout for consistent waiting fairness. No document reset,
schema migration, quota change, or automatic pending-permit recovery is involved.

Schema 3 uses a global scope instead of legacy per-source-project documents.
Running old project-budget readers alongside new global-budget readers would
create independent allowance and violate the intended total. **Do not mix legacy
per-project readers with global-budget readers or simply change namespace,
database or quota project.**
This code change does not implement any production migration or permission change.

Before a separately authorized migration, disable admission and drain/stop every
old reader instance/revision. Establish that no reserved old SDK call can still
run (an HTTP timeout is insufficient), then wait at least 60 seconds after the
last possible call. Only then may an operator enable the consistently configured
new readers. Preserve old records for review; the new code does not reset them.
If the last possible call cannot be established, remain disabled. A rollback to
old readers needs the same drain-and-wait discipline.

There is no Firestore TTL or automatic deletion/reset of permits. A crashed
process or failed finalization may leave a permanent pending slot; 12 unresolved
slots stop the viewer globally. Recovery separately requires draining all readers,
establishing no reserved calls remain possible, waiting 60 seconds after the last
possible call, and an explicitly authorized operator reset. Waiting FIFO entries
can expire; that must never be confused with permission to expire pending permits.

## End-to-end deadline and cancellation

One monotonic request budget starts in the route before JSON parsing, dependencies
and worker-pool queueing. It includes catalog validation, cold SDK/ADC construction,
coordination, Logging and local work. Defaults and validated ceilings:

- Server asynchronous wait: 20 seconds (range 4–20).
- Reserve phase: 4 seconds (1–6), including all reads/writes/conflicts.
- Logging RPC phase: 10 seconds (1–12), including cold setup.
- Finalize phase: 4 seconds (1–6), including all reads/writes/conflicts.
- Browser wait: 25 seconds, allowing five seconds response/transport margin.

Phase ceilings must leave at least one second inside the total for local work;
non-finite/nonpositive/incoherent values fail startup. RPC/local work preserve the
finish allowance and check the same shrinking deadline. A detected cancellation
before dispatch prevents it. Staged retention changes are not published if their
work deadline expires. No operation starts with zero/negative remaining time.

HTTP timeout/cancellation stops waiting, but Python cannot kill a synchronous
SDK/ADC thread or prove a remote operation stopped. The worker retains its cache
lock and pending permit. Only that owner may finalize after known completion,
within the original total deadline. A late worker does not start finalization,
and the pending slot stays charged. No cancelled waiter refunds a permit. Browser
abort after the initial disconnect check may not cancel the ASGI task; the server
deadline still applies. Event-loop stalls, scheduling and network delivery can
add latency outside the bounded wait. These are ceilings, not hard real-time SLAs.

## Memory, redaction and limits

Across the entire replica, retained serialized entry data is at most 2000 entries
and 2 MiB, not 2 MiB per resource/project. Eviction first removes from the resource
using the most entries/bytes, with LRU ties, preserving sparse sources against a
noisy neighbor. If even one entry per source exceeds the global bounds, some
resources lose retained entries and explicitly report cache eviction. Metadata is
bounded to 4096 local resources with LRU eviction; removed or changed catalog
resources are collected before reads. Historical project churn does not accumulate
permanent caches. Replica restarts and metadata eviction reset local observations.

These retained-data bounds do not include Python object overhead, bounded cache
metadata, in-flight requests or raw first-page SDK memory. A provider page can
contain larger unsanitized data before normalization. Each sanitized entry is
bounded to 16 KiB; oversized payloads become an explicit size notice. Sanitization
has depth/node/string/aggregate limits, redacts nested secrets/headers,
bearer/JWT/PEM data and credential-bearing URLs, and removes control/ANSI and
bidirectional characters. The UI renders text, never raw HTML. Defense in depth
cannot recognize every novel secret format: producers must not log credentials.

Two-minute overlap can miss very late entries. A first page can omit older busy
logs. Caches differ across replicas. The user may wait for a new local sample
after being routed elsewhere. No full historical completeness, lossless delivery,
complete ordered stream or guaranteed observation interval is promised. Use the
authorized Explorer link for deeper investigation.

## Offline verification

Tests use signed synthetic OAuth, synthetic quota-attributed credentials and
version-preconditioned Firestore doubles, without ambient credential discovery or
production reads. Coverage includes singleton filters for 500 resources/projects,
noisy/quiet isolation, cache/policy invalidation during I/O, global memory bounds,
project churn, queue overload/expiry, FIFO across replicas/resources, deadlines,
failed/ambiguous calls and indefinitely pending permits. Multiprocess quota tests
use one genuinely shared synthetic CAS store, never a copied per-process fake.

Run repository CI plus `tests/test_log*.py` and `tests/test_runtime_logs.py`; see
`AGENTS.md` for the exact-commit `oss-v2` release gate. Frontend fake timers cover
the 25-second timeout, Retry-After, queue/age states and safe text rendering.
Offline tests do not verify production IAM, provider connectivity, rollout
readiness, real latency or the required exact-commit release evidence.

References: [Cloud Logging Python client](https://docs.cloud.google.com/python/docs/reference/logging/latest),
[entries.list](https://docs.cloud.google.com/logging/docs/reference/v2/rest/v2/entries/list),
[Logging quotas](https://docs.cloud.google.com/logging/quotas),
[Cloud Run logs](https://docs.cloud.google.com/run/docs/logging),
[Logging access control](https://docs.cloud.google.com/logging/docs/access-control),
[Firestore transactions](https://docs.cloud.google.com/firestore/native/docs/manage-data/transactions).
