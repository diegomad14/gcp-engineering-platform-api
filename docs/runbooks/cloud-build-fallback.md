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
