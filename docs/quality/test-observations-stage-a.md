# Stage A: test observations, never test reuse

This additive channel preserves observational evidence for future investigation.
It does not authorize skipping a command, change exact-SHA eligibility, change
`oss-v2` or report v2, reduce a coverage threshold, or alter release planning.
The current full pytest/security/Smarti/integrity checks still run according to
their existing profiles. A failed preflight produces no reusable evidence.

## Boundaries

- Python pytest has an observational plugin on the primary test command only.
  It does not modify collection, markers, reports, or exit status. The profile's
  command and arguments are unchanged. Extra checks have their original environment.
- Repository code can tamper with its own plugin output. These are untrusted
  observations, including coverage and dependency-content measurements.
- The trusted executor exports an allowlisted sidecar after all existing checks.
  Missing, unsupported, malformed or oversized evidence is unavailable. Export
  failures cannot change the canonical report, report digest or quality outcome.
  The previous sidecar is cleared before export. A strict local wrapper binds
  execution ID, fingerprint, provider run and report digest; the publisher rejects
  stale bindings before obtaining credentials or making a request, including
  when cleanup failed. The wrapper is removed before endpoint ingestion.
- The credentialed publisher submits the ordinary canonical callback first, then
  makes one best-effort observation attempt. Identity-token and observation HTTP
  requests each have a five-second timeout. There are no observation retries.
- The separate private `test-observations` route uses exactly the existing
  provider/run/source verifier and event token. It binds the body to the same
  server-side execution and the previously accepted canonical report digest.
  It adds no identity, token, IAM grant, public read API or external blob URL.
  Its body is stream-limited to 1,000,000 bytes before JSON parsing; the existing
  event callback's 1 MB limit is unchanged. This route does not invoke lifecycle
  mutation, reconciliation, release planning or the ordered event queue.

## Data minimization and bounds

Observation schema v1 uses an exact field allowlist and canonical ASCII JSON,
limited to 700,000 bytes. Coverage input is at most 16 MiB and 4,096 tracked
Python source files with 300,000 total line numbers. Source names are canonical
repository-relative paths; outside files, symlinks, traversal, duplicate aliases
and untracked paths are rejected. Only executed/missing/excluded line numbers
are retained. Coverage contexts, function labels, timestamps and host paths are
not exported.

The test manifest contains at most 20,000 SHA-256 node IDs, a collection digest,
setup/call/teardown outcome enums, collection-error/deselection counts, completion
and exit status. Raw parameter IDs, exception text, skip reasons and logs are
never exported. Duplicate/retried reports mark collection incomplete rather than
silently collapsing execution history. No collection-only rerun is introduced.

Installed dependency **file bytes**, not just RECORD declarations or package
versions, are hashed. Names, paths, versions and source URLs are not exported.
The measurement is capped at 100,000 files, 256 MB and a three-second read loop;
resource limits, symlinks, traversing RECORD entries, missing files and direct
URL/editable metadata mark it incomplete. This is a content observation, not a
verified wheel/archive digest or reproducible locked environment. Import/native
system libraries, network effects and mutable package resolution remain gaps.
The plugin's finish-measurement and exporter overhead is recorded as
`measurement_ms`; it does not claim to measure all hook overhead or time saved.

Source-tree identity is obtained from the existing trusted Git checkout. Config
and fixture manifests contain only content digests of allowlisted tracked paths
(config files, requirements files, conftest and fixture/testdata/snapshot dirs).
The test command is hashed. Runtime data is a Python version and fixed
implementation/OS/architecture enums. Database DSNs, environment variables,
credentials, tokens, hostnames and raw fixture contents are never exported.
Database engine/version and external fixture state are **not independently
attested** by this first cut; matching Git fixture manifests cannot close that gap.

## Storage and shadow comparison

The existing quality bucket/local backend is reused under `test-observations/`.
The server seals repository/service/head/base, execution fingerprint, provider
run, executor digest, profile/policy hashes, operation and accepted report digest
from its own execution state. The receipt labels measurements as untrusted.
A receipt is not a signature proving the tests ran or that execution was hermetic.
Existing permissions must cover this prefix; denied storage remains a visible
`publish_unavailable` outcome. No permission expansion is attempted.

A conditional execution-index claim happens before a content-addressed blob is
written. GCS uses generation-match-zero; local tests use exclusive creation.
Different-content replays cannot allocate more blobs for that execution.
Identical retries are idempotent and can finish an interrupted blob write.
The index contains the full bounded receipt, so the digest can be checked even
if the duplicate content blob write was interrupted. Index identity, receipt
shape and embedded content digest are checked on internal reads.

`shadow_for_executions` explicitly compares two server-stored execution receipts.
This first cut does not auto-select candidate PRs, add a scheduler, or change
which CI executions run. It reports missing/invalid/stale/revoked data, replay,
source/head/base/dependency/runtime/profile/policy/config/fixture/command/test/
coverage differences and incomplete/failed/skipped tests. In every case,
`eligible=false` and `reuse_allowed=false`, with `stage_a_shadow_only`,
`untrusted_measurements` and `hermeticity_unproven` reasons. TTL can only add an
ineligibility reason; it never activates a cache. Historical aggregate reports
cannot be retroactively converted into detailed observations.

## Activation and remaining work

The Dockerfiles package these helpers, but this change does **not** build, publish,
retag or deploy executor images or alter any current immutable image pin. A
separate reviewed tooling rollout is required before existing pinned executors
emit this sidecar. Old publishers and old reports remain compatible. The ordinary
PR CI evaluates code; it is not evidence that the pinned production executor has
already adopted this new feature.

A future reuse proposal would need separately approved policy, trusted execution
attestation, locked/resolved dependency provenance, exact source/base semantics,
attested database/runtime fixtures, complete outcome and coverage evidence, and
safe access to Git metadata. This patch changes no Git-directory permissions and
does not resolve existing restricted-HEAD visibility limitations. It provides no
performance saving by itself and no approval for enabling actual reuse.
