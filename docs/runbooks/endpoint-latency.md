# Endpoint latency verification

## Scope and safety

This change isolates synchronous provider calls from the ASGI event loop, bounds
repeated GitHub circuit propagation and removes redundant summary work. It does
not change deployment authorization, database permissions, Logging quotas,
private catalog distribution or the candidate/promote release process.

GitHub webhooks and release callbacks share one dedicated event worker per
process. This preserves their previous in-process serialization while allowing
the event loop and the ordinary API worker pool to serve other requests. Work
already admitted to that queue is not cancelled when its HTTP caller disconnects.
Admission is bounded to 32 events per process, including the event running on the
worker. A full queue immediately returns a generic HTTP 503 with `Retry-After: 5`
without submitting the event or waiting for an ordinary API worker token. A
cancelled HTTP waiter retains its slot until the admitted processing actually
finishes; provider errors and real queued-future cancellation also release the
slot. Slot release does not depend on the caller's event loop remaining open.

An event rejected at capacity has not been processed and requires redelivery by
its caller. `Retry-After` is advisory. [GitHub does not automatically redeliver
failed webhook deliveries](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries):
operators must redeliver manually or use an already authorized redelivery
automation. This change does not activate automatic retries, create a redelivery
automation or acknowledge events early.

Do not use deployment GETs as synthetic probes: some reads reconcile state and
can start failover. Do not replay webhooks, run load tests against production,
clear production caches or disable catalog revalidation to measure performance.

## Offline regressions

- Hold a fake provider with a threading event. While the first request remains
  pending, verify a second HTTP request responds on the same ASGI application.
  Release the event and verify the original response waits for persistence and
  reconciliation. Invalid signatures, body limits, denied identities and replay
  handling must retain their response and no-provider-call behavior.
- Fill all event admission slots using controlled offline futures. Verify a
  further event receives 503 before submission, and cancelling its HTTP waiter
  does not free an admitted slot prematurely. Verify capacity returns after
  actual completion, provider failure, queued-future cancellation, submit
  failure and completion after the caller's event loop has closed.
- Verify catalog revalidation still fails closed when the source changes or
  becomes unavailable before the response. Offloading changes execution context,
  not the ordering or requirement of the authorization checks.
- Simulate concurrent circuit readers, failed propagation, an abandoned lease,
  a successful probe and stale completion tokens. Provider decisions always read
  current persisted circuit state. Only best-effort repository-mode propagation
  is coalesced across workers using the existing circuit document.
- Verify a slow billing key cannot block another key or cache hit. Concurrent
  callers for the same key share one result or error; a failed load can retry.
  Keep the catalog identity and Bogota calendar date in the cache key.
- Count consumed release iterator elements and billing queries with fake clients.
  Compare complete, incomplete, mixed-currency and missing-data DTOs. Savings must
  never be inferred from unequal usage windows or missing exported components.

Run the repository CI checks and the unchanged `oss-v2` gate. A local test run is
not the signed normalized evidence for a final published commit. Candidate and
promotion both require evidence for the exact service, repository and SHA.

## Production comparison after an authorized release

1. Record current and candidate revision, artifact digest, commit SHA and traffic.
2. Read existing Cloud Run request logs for API and web over the same bounded
   interval. Group by method, normalized route and revision. Report request
   count, statuses and empirical p50/p95/p99/max; label small samples and routes
   without traffic. Separate successful requests from quick authorization or
   provider failures when comparing releases.
3. Pair web and API requests using the same trace, method and path. Compute
   per-request proxy overhead before calculating percentiles. Do not subtract
   unrelated percentile distributions or count both layers as separate actions.
4. Compare Cloud Run elapsed time with `request_complete duration_ms` on the same
   instance and corresponding completion. The difference is time outside the
   application timer, not a measured Firestore, GitHub or lock duration.
5. Check event-loop responsiveness during naturally occurring webhook traffic.
   Review instance startup, per-minute CPU/memory and concurrency alongside the
   request timelines. Low minute averages do not exclude shorter resource peaks.
6. Observe the shared circuit document without changing it. An event during
   cooldown should not perform a repository sweep. An effective sweep claims and
   completes its lease. Failed sweeps become eligible after 60 seconds, successful
   sweeps after 600 seconds, and abandoned leases after 300 seconds; each requires
   a subsequent real event. These delays do not close the safety circuit.

Do not claim a production latency improvement from unit-test timing. Confirm it
with comparable natural traffic after rollout; extend the observation window
when a route has no meaningful sample. Provider-level time attribution requires
additional spans rather than inference from overlapping requests.

## Remaining limits

Each event request continues to wait for its result. The event worker queue is
process-local, not an early acknowledgement or durable webhook queue. Existing
transactional build-submission and callback-sequence claims remain in force;
GitHub check creation still has a pre-existing cross-replica GET/create race that
the in-process queue does not solve. A circuit propagation sweep longer than its
lease can overlap a new sweep; the repository-mode writes are idempotent and the completion token
prevents one sweep from releasing another's lease.

Overview still reads historical deployment records, and quality still reads
per-service evidence and may fall back to GitHub. More invasive query/index or
catalog caching changes need separate correctness and authorization review.
Private catalog reads and negative management-capability barriers remain fresh.
MCP authorization service internals retain some synchronous store calls in async
methods; the current patch does not refactor the MCP provider lifecycle.
