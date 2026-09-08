# Cloud Build fallback: bounded runtime and cost

This Python-service build engine is an implementation of an authorized fallback,
not an authorization to bypass GitHub, release approval, or candidate/promotion
checks. It builds and publishes an image; it never changes Cloud Run traffic,
production capacity, databases, schedulers, or queues. The previous release
procedure remains available for recovery. SanPlat's continuous deployment caller
uses this engine for API builds; its existing web builder remains unchanged.

## Prepare and submit once

Use a committed Engineering Platform checkout and a reviewed service commit.
The central catalog supplies repository, project, region, image destination,
quality policy and coverage thresholds. Arbitrary threshold/machine overrides
are not accepted. Preparation ignores dirty and untracked service files by
exporting a Git bundle and archive of the exact commit and its comparison base.

```bash
python scripts/ops/cloud-build-fallback/prepare.py \
  --source /path/to/cgm-sanplat-api \
  --service cgm-sanplat-api \
  --sha FULL_RELEASE_SHA --base-sha FULL_BASE_SHA \
  --profile performance \
  --evidence-uri gs://cgm-sanplat-data/corporate-release/fallback \
  --install-command 'python -m pip install --no-build-isolation -e ".[dev,sqlserver,postgresql]" "ruff==0.15.22"' \
  --output /path/to/evidence/performance

python scripts/ops/cloud-build-fallback/control.py submit \
  --build-dir /path/to/evidence/performance \
  --state-file /path/to/evidence/performance.state.json \
  --experiment /path/to/evidence/experiment.json --slot performance
```

`performance` uses the existing default-pool `E2_HIGHCPU_8` with four pytest
workers. `economy` uses default-pool `E2_STANDARD_2` with two workers. Both use
`worksteal`, the same SHA/base, pinned tooling, complete suite and merged coverage.
Historical timings balance the initial contiguous worksteal blocks. Each worker
starts a different long case. This only permutes collected tests: it never skips
cases, replaces assertions or changes PostgreSQL durability. Timings are hints
from the same repository, including for future commits; new test IDs receive a
median estimate and remain in the suite. Every worker records the collection
hash, scheduled-order hash, known/unknown counts and measured/current source SHA.
Repositories without a duration profile retain normal worksteal ordering.
Source preparation, tooling and PostgreSQL start concurrently; the runtime image
build overlaps quality. Publication checks the canonical `oss-v2` policy, report
hash and Docker revision/source labels. There is exactly one push.

No persistent build cache, private pool, larger VM, additional disk or periodic
build is introduced. Builds have a 1,256-second timeout, equal to the reference
execution duration rounded up. Ordinary command failures reach the final evidence
uploader and fail the build. Provider cancellation or timeout can interrupt the
uploader; Cloud Build status/logs remain authoritative and missing evidence blocks
publication/consumption.

Each preparation has a unique attempt ID, image tag, evidence prefix and hashed
request. Do not edit the prepared directory or place state/evidence files in it.
The controller records intent before submission and retains a canonical adjacent
`<prepared>.submission.json`. A different state filename still recovers that
attempt. If the client disconnects, run `status` with the same prepared directory
and state file. Never delete state to retry. An uncertain submission searches
Cloud Build for the exact request fingerprint and never resubmits automatically.

The optional experiment ledger permits one `performance`, one `economy` and one
`confirmation` attempt; failed or uncertain submissions consume their slot.
Confirmation requires a fresh preparation of the same SHA/base. The ledger and
all state files must remain outside the immutable input directory.

## PostgreSQL and quality evidence

`postgres_workers` creates a unique disposable database for each FND/corporate
pytest worker. WM retains the required database name `wm_test`; its existing
per-test UUID schemas isolate tests concurrently. No production DSN is accepted.
Workers clean up their own databases, and the temporary PostgreSQL container ends
with the Cloud Build VM. All source tests remain unchanged; the plugin is outside
the source/coverage roots. Actual concurrency and timed-provider tests are retained.

Reports require the exact service/repository/SHA/base, catalog thresholds, unique
check categories and all mandatory checks passed. A differential `SKIPPED` result
is valid only for zero applicable changed lines under the canonical policy.
Detailed command output is written once to the report directory, with short
progress every 30 seconds and a bounded tail in Cloud Logging.

To register a successful build's same report in Engineering Platform, install the
platform Python dependencies, provide `QUALITY_API_TOKEN` through the existing
secure credential flow, then run:

```bash
python scripts/ops/cloud-build-fallback/control.py register-quality \
  --build-dir /path/to/evidence/performance \
  --state-file /path/to/evidence/performance.state.json \
  --quality-api-url https://eng-platform-api-pzzhmu7una-uc.a.run.app
```

This verifies successful build identity, the uploaded report hash and canonical
policy, and registers the report idempotently. It does not repeat the suite or
promote the release. Do not include credentials in CLI arguments or evidence.

## Measure cost and select a profile

Reference: SanPlat WM SHA `d262a099f3305433f881b1da0a3c8325ec69a3b4`, base
`0d512eb38b68245ce919fb1d35f9b724ce771866`, Cloud Build
`4876dcd7-19d9-46f3-addc-2f82b0b1338c` in `us-central1`.
Execution was 1,255.71 seconds, queue 101.79 seconds and the test check 1,012.951
seconds (1,052 passed, one skipped). The report rounded global coverage to 78.34%.

At 2026-09-08 list rates, reference compute is approximately USD 0.3265.
`E2_HIGHCPU_8` is USD 0.0156/minute and `E2_STANDARD_2` USD 0.006/minute.
Queued time is excluded and partial minutes are billed per second. Do not assume
free-tier availability. Sources: [Cloud Build pricing](https://cloud.google.com/build/pricing),
[Artifact Registry pricing](https://cloud.google.com/artifact-registry/pricing).

`measure.py` consumes completed build snapshots and explicit auxiliary cost inputs
per build ID: `storage_usd`, `logging_usd`, `transfer_usd`, `operations_usd`.
Missing costs block final selection, including missing costs for failed attempts.
Document measured bytes, retention period, tariff date and any estimates alongside
the cost input; a list-rate model is not an invoice. Include source archives,
images, evidence, logs and transfer, without double-counting shared image layers.

```bash
python scripts/ops/cloud-build-fallback/measure.py \
  --build /path/to/evidence/performance.state.build.json \
  --build /path/to/evidence/economy.state.build.json \
  --costs /path/to/evidence/auxiliary-costs.json \
  --monthly-releases 30
```

The monthly count is an explicit equal-volume scenario, not a forecast. The
experiment total includes failed attempts and stays separate from recurring cost.
Select the lowest complete estimated cost that also finishes execution within
628 seconds; break equal-cost ties by execution duration. The new *complete* cost
must be no greater than the old compute-only USD 0.3265 ceiling, a conservative
comparison even before adding old auxiliary costs. If no profile qualifies,
continue test optimization instead of increasing resources or relaxing checks.

Record queue, build, candidate preparation, drain, maintenance, promotion and
functional validation separately. SanPlat's continuous path does not add a new
drain/maintenance window; legacy corporate windows retain their existing protocol.
Referenced frontend backend tags must remain Ready and present before preparation
and promotion. Production maintenance acceptance is observed at the next
authorized release; benchmark builds do not move traffic.

## Validation record

Local exact-source suite completed with 1,052 passed and one skipped in 475.57
seconds on Apple M1, Python 3.11.14 and Docker PostgreSQL 16. Peak aggregate process
RSS was 915 MiB. This is feasibility evidence, not the Cloud Build comparison.
Local coverage was 78.3053%; compare raw coverage files before interpreting small
differences from the rounded baseline report. Cloud results and final selection
are recorded in the companion performance evidence after the bounded experiment.

The initial cloud experiment on engine `b7611e02d596bfe5a0234f346c47859347822827`
found an important scheduling bottleneck. With four workers, worksteal placed
both consecutive 7,911-meter cases in the same initial block. They cost 404.46
seconds (SQLite) and 225.49 seconds (PostgreSQL) in Cloud Build, effectively in
series. The versioned duration profile and balanced initial order correct this
without changing either case or the application commit.

| Initial profile, before balanced ordering | Build | Execution | Result |
|---|---|---:|---|
| E2_HIGHCPU_8, four workers | `763cfa67-593d-426d-b723-2b24384641a7` | 900.283 s | Quality passed; time objective missed |
| E2_STANDARD_2, two workers | `3bc1e4b9-7ccf-43b7-ba02-854890ecf19a` | 1,283.373 s | Provider timeout; publication did not run |

The successful initial build retained 1,052 passed, one skipped and raw global
coverage 78.3425% (reference 78.3363%); all canonical oss-v2 checks passed. Queue
for that build was 53.580 seconds and is excluded from cost. Its compute estimate
is USD 0.234074; the timed-out economy attempt adds USD 0.128337. Their combined
exceptional compute is USD 0.362411, before storage, logging, operations and
transfer. These are not complete invoice costs and neither initial profile is
an accepted winner. Provider cleanup can make reported execution exceed the
configured 1,256-second timeout; accounting uses the full reported interval.

No automatic retry or winner confirmation was launched. Timing and complete-cost
acceptance of balanced ordering remain to be measured in a separately authorized
cloud run or the next authorized release. The existing procedure remains available
for recovery. Do not infer a 50% cloud improvement from a local scheduling model.

The completed local balanced-order validation retained the exact 1,053 collected
identities: 1,052 passed and one skipped in 403.12 seconds (403.882 wall seconds),
15.2% below the earlier local run. Coverage was 78.3456%, with 13 additional lines
and none lost against that local run. Peak process RSS was 714 MiB. Actual large
case durations increased when run together; the simulated 235-second makespan
was not observed. PostgreSQL settings and real timing assertions stayed intact.
The final engine commit `edc0a9666eb45841e4ffe5a3b0f731d2b4e7d33e` passed its
exact-commit normalized gate on Python 3.12: 403 tests, 88.89% global coverage and
all required lint, format, type and security checks. SanPlat pins that engine SHA.

### Complete cost model for the initial experiment

The following are conservative list-price models, not invoice amounts. Retain
full images, source and evidence for the stated horizon, without free credits,
layer deduplication or assumed cleanup. AR cleanup is in dry-run; GCS source is
Standard US multiregional and evidence is Standard us-central1. Scanning is
disabled. Use AR USD 0.10/GiB-month and GCS USD 0.026/0.020/GiB-month, divided by
730 hours. Logging is USD 0.50/GiB. Transfer and operations use conservative upper
rates of USD 0.23/GiB sent, 0.02/GiB received and 0.010 per 1,000 requests.
Sources: [GCS](https://cloud.google.com/storage/pricing),
[Logging](https://cloud.google.com/products/observability/pricing).

Mature project counters for 19:50–20:15 UTC measured 2,813,461 log bytes, 109 GCS
requests, 143,541,028 sent bytes and 6,652,168 received bytes. Charge that entire
window to each attempt, including unrelated traffic: logging USD 0.00131012,
operations USD 0.00109 and transfer USD 0.030871. This deliberately overcounts
shared traffic. Performance stored a 96,821,058-byte image, 2,216,684-byte source
and 32 evidence objects totaling 2,209,679 bytes. Economy stored its 2,216,682-byte
source; its image publication and evidence upload did not run.

| Initial attempt | Complete 30-day model | Complete 90-day model |
|---|---:|---:|
| Performance, successful but too slow | USD 0.276332 | USD 0.294306 |
| Economy, timed out | USD 0.161661 | USD 0.161767 |

The successful attempt's 90-day bound remains below the old compute-only
USD 0.326485 ceiling, although its time fails acceptance. Both initial attempts
total USD 0.456073 with 90-day retention and the shared window counted twice
(USD 0.422802 when counted once). Later diagnostic requests outside the captured
window are additional exceptional costs, not measured here; they are not zero.

For one release at the start of each day, integrate storage as
`daily_storage_per_release * T * (T+1) / 2`, then add T times non-storage cost.
At equal volume, the initial performance model is USD 8.1596 for 30 releases in
30 days and USD 25.2878 for 90 in 90 days, below even the corresponding old
compute-only USD 9.7945 and USD 29.3836. After 90 releases the existing stock adds
USD 0.8088 for another 30 days if retained, before new releases. Lifetime storage
cost remains unknown. Economy cannot model delivered releases from a timeout.
There is still no selected winner, and these initial costs do not certify the
balanced-order engine's future cloud duration or billing.
