# Firestore reconciliation and history reads

This change is code only. It does not deploy a revision, create an index, migrate
documents, change an executor/default, or change reconciliation frequency.

## Preserved behavior

Reconciliation exhausts snapshot-cursor pages before restoring global oldest-first
order. The scheduler uses `iter_due()` and `iter_unfinished()` instead of stopping
after the former 100/50 requests. Bounded list wrappers remain for callers that
explicitly request a list. Transport page size is not a work limit.

Release candidates include unknown/nonstandard nonterminal states, main quality
awaiting its planner, and the existing bounded planner-recovery exceptions. Passed
PR quality and the immutable bootstrap metadata fence are excluded. The recovery
predicates and permissions are unchanged; these queries never submit a build.

All candidate queries and history counts/pages use the same server `read_time`,
including across repository aliases. Snapshot cursors retain document identity
when timestamps tie. History and latest use descending timestamp and document-id
ordering, matching the durable writer's id and local-reader tie breaker. Total
counts are server aggregations, not counts of the returned page.

Missing legacy fields are not silently omitted. Snapshot-consistent aggregations
detect missing/null status or nonstandard/missing release operation. An explicit
warning identifies compatibility queries/scans; missing history dates trigger a
paginated scan scoped to that repository/service. Defaults and validation errors
are preserved. The known immutable bootstrap fence is read by its fixed ID when
needed and does not force scanning the archive. No exception handler turns an
index/permission failure into a full scan.

The SDK and lock now require Firestore 2.34.1, whose query, aggregation and document
read APIs support `read_time`. Firestore does not provide an unbounded historical
snapshot: a scan exceeding the provider's read-time retention fails explicitly
rather than silently moving to a new snapshot. No wall-clock timestamp is used as
a substitute for the server snapshot.

## Index review before deployment

The effective database/index inventory has not been inspected with GCP credentials
in this cloud environment. Resolve the actual collection names from the existing
configuration and check these collection-scope indexes before deployment:

| Collection configuration | Composite fields |
| --- | --- |
| `ENG_PLATFORM_DEPLOYMENT_FIRESTORE_COLLECTION` | `service_name ASC`, `created_at DESC` |
| `ENG_PLATFORM_RELEASE_EXECUTION_FIRESTORE_COLLECTION` (default `release_executions`) | `repository ASC`, `created_at DESC` |
| `ENG_PLATFORM_RELEASES_FIRESTORE_COLLECTION` | Existing `service_name ASC`, `created_at DESC` used by release history |

Document identity follows the last ordered field, so `__name__ DESC` uses the
normal descending suffix. Status/operation membership and inequality queries
require their normal scalar indexes. The planner and identity lookups combine
equality filters; validate their plans/index exemptions too. Firestore supports
index merging for equality filters, but local fakes cannot establish effective
production index availability. See the [official index documentation](https://firebase.google.com/docs/firestore/query-data/index-overview).

These are proposed index shapes, not instructions already executed. If anything
is absent, present the exact collection/database/index and existing-operator
permissions for approval before creating it. Do not deploy and rely on fallback.

## Local measurements and remaining limits

Provider-shaped MVCC fixtures count documents delivered to the application:

| Case | Previous documents | New documents |
| --- | ---: | ---: |
| Release reconciliation, retained terminal/passed-PR history | 6,459 | 259 (257 due; two recovery candidates rejected locally) |
| Deployment reconciliation | 5,453 | 453 |
| Deployment history, requested 20 | 3,000 | 20 |
| Latest for two services | 3,001 | 2 |
| Counts | Every matching document | 0 document payloads |

Release reconciliation adds three count aggregations; deployment reconciliation
and history add two. Latest uses two collection-wide aggregations and at most one
document per requested service on the normal path. Legacy compatibility is more
expensive by design, and may require archive/service scans. A bootstrap-fence
exception may require fixed-ID point reads. These are not zero-billable-read
claims: aggregation index-entry reads, empty-query minima, point reads and extra
round trips remain. No production operations or monetary savings were measured.

Latest now issues a bounded query per service rather than downloading grouped
service histories. Measure overview latency at catalog scale before deployment.
Exhaustive reconciliation also removes the former per-sweep work cap, so measure
request duration and provider polling for a large active backlog. Do not discard
old/unknown requests to satisfy a latency target. Concurrent writes are visible
on the next sweep; each current sweep/history result remains at its server
snapshot. Observation-only management barriers are unchanged and tested.
