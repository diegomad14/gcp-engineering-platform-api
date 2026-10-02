# Smarti mandatory CI: coordinated rollout

The mandatory-CI implementation was merged in PR #117 and released as v0.32.0,
commit `8c3bed960a179b1c8a8e7513e046385a4640d7cc`. Its quality images were built
and published by the official tooling workflow. Production activation and fresh
Smarti browser/PostgreSQL evidence are still separate steps. This rollout does
not change FinOps, alert delivery, IAM, release permissions, or coverage thresholds.

## Required evidence

- `cgm-sanplat-web` and its `cgm-artemis-web` alias gain a blocking `smarti_ux`
  extra check. The existing tests, build, proxy contracts, scanners, global 70%
  coverage, changed-line 80% coverage, and 3,600-second deadline remain.
- `smarti_ux.py` requires Playwright 1.62.1 and exactly six cases: desktop and
  mobile in each of `smarti-prevention.ux.spec.ts`,
  `smarti-source-warnings.ux.spec.ts`, and `smarti-calendar-coverage.ux.spec.ts`.
  The existing specs contain the axe assertions. Missing files, skips, focused
  tests, expected failures, retries, duplicate results, collection failures, and
  execution failures block the result. JSON evidence is retained in
  `quality-reports/smarti-ux.json`.
- The node image installs the pinned Playwright Chromium revision 1234 and
  version-matched Linux dependencies at trusted build time. Browser files are
  read-only under `/opt/eng-platform/playwright-browsers`. Runtime downloads are
  disabled. Repository commands still execute as UID/GID 65532 without cloud or
  GitHub credentials; the existing supervisor, capabilities and mounts remain.
- `cgm-sanplat-api` and its `cgm-artemis-api` alias gain a blocking
  `smarti_postgres` extra check. The controller requires a plain `postgresql://`
  URL for a loopback, port-5432 `smarti_test` database. The full suite retains
  its existing tests and thresholds. The extra check separately requires both
  Smarti modules, at least one standalone PG case, twenty publication PostgreSQL
  variants, and twenty SQLite variants (41 total currently), matching complete
  collection and execution inventories with no skips or deselection.
- Temporary PG provisioning creates and probes `smarti_test`. Each xdist worker
  receives its own Smarti database URL, independently of the existing WM/FND
  databases. Worker environments remove inherited host/service overrides. The
  controller database guard applies before per-worker fixture assignment; it
  does not reject the intentionally distinct worker database names.
- Canonical report validation requires one consistent mandatory result per
  declared category. Missing, duplicate, skipped or nonblocking-failure evidence
  is rejected. Failed checks can still be recorded as failed evidence.
- The API-only portable fallback passes a validated `--extra-checks-json`, uses
  its frozen trusted helper, and includes its input hash in the request
  fingerprint. Default empty extras avoid duplicating canonical execution.
  Fallback result validation and registration reject missing, duplicate, failed,
  skipped or empty Smarti PostgreSQL evidence. It cannot turn an old report
  containing only SQLite passes into new mandatory PG evidence.

## Compatibility and hashes

The aliases share their canonical profile hashes:

- Web: `b2db09efe6927099f4c9501b88a4e91325fe928585fbdd83e34280a0a667bd86`
- API: `64bfb83edbd9b07c8a98c4d6764a4a3dde506c3f84539105fa84977508e455f4`

Both change from the previous contract. Historical green results, including
API evidence produced while PostgreSQL tests skipped, are not sufficient under
these hashes. The old node image digest does not contain Chromium or the new
helpers and must not be paired with the new manifest.

Before activation, inventory every affected canonical/alias catalog identity
and each open PR head, including independently evolving UI migration heads.
Confirm all three UX specs, matching Playwright lockfile version, and both PG
modules are present with the required cases. Coordinate merges/rebases first.
Older base/main snapshots or sibling PRs missing these files will correctly
fail; do not add a skip or silently make the requirement advisory to accommodate
them. Unrelated service profiles do not acquire Smarti checks, although shared
images contain the trusted helpers.

## Review and rollout sequence

1. Review the complete local patch and test evidence, including the new portable
   extra-check parser and fallback rejection paths.
2. Obtain separate approval for central publication, remote image build/push,
   SAST metadata egress if needed, and activation. Local preparation alone does
   not authorize those actions.
3. In an authorized isolated build, build both quality images. Run image unit
   tests and adversarial supervisor tests. The real Smarti browser/axe and PG
   cases remain mandatory application evidence: run them with the new coherent
   contract in step 5, not with an old image or an overridden/advisory profile.
4. Record the resulting immutable OCI digests and OS package inventory. The
   versioned Playwright installer selects dependencies; the reviewed OCI digest
   freezes resolved Debian packages. Do not reuse the old cached digest or an
   unversioned image tag. Any change to browser/package pins requires rebuild
   and renewed real-image evidence.
5. Coordinate the control-plane manifest and executor digests so both authorize
   exactly the same hash. A mismatch must fail closed. Trigger fresh quality
   execution on each reviewed compatible Smarti head. Launch the six real
   browser/axe cases as UID 65532 with the normal mounts/capabilities and no
   tokens, and verify PG through the actual canonical/fallback execution path.
   Verify mandatory categories plus all existing gates before releasing or
   deploying the Smarti applications.

## Versioned bundle and candidate guard

`src/eng_platform_api/quality_executor_bundle.json` records the reviewed node
and python OCI digests, tooling source SHA and SHA256 of the exact profile
manifest. The recorded tooling source identifies the published images; it is
not the commit SHA of a later central release adding or updating this guard.
The manifest bytes must remain identical to the reviewed bundle, or the
bundle and images must be reviewed and rebuilt together.

The existing `verify_candidate_config.py` hook runs from the exact tagged
source after candidate smoke and before production promotion. It remains
read-only. It checks the exact revision name, existing secrets writer, manifest
hash, and both literal unique quality-image environment values against the
bundle. Missing, duplicated, partial, stale or secret-reference pins fail
closed. JSON duplicate keys are rejected, including nested image/profile
objects and provider data. Manifest semantics are checked by the unchanged
executor validator from the same tagged checkout; a missing validator also
fails closed. This release hook is source-checkout tooling, not a standalone
API-image command. It prints only a fixed PASS/FAIL result; it never repairs configuration,
pulls an image, exposes provider diagnostics or reads secret contents.

The hook name and signed deployment profile do not change. No new caller
inputs, credentials, IAM grants or executor-image changes are introduced by
this guard. Its source, tests, workflow and bundle require their own PR, exact-commit OSS
evidence and semantic-release tag before use; v0.32.0 only checked the writer.

### Coordinated preparation inside the official MCP deployment

1. Start the new guarded, eligible central tag through Engineering Platform's
   documented `start_deployment(service_name, tag, reason, idempotency_key)`.
   The backend selects the executor and verifies the deployment permission.
   Read-only metadata does not prove the `eng-platform.deploy` scope. A fresh
   permission denial blocks the operation; no direct CLI or simplified-MCP
   deployment is a substitute. A separate operator CLI login is not needed
   when the existing authorized workflow/WIF can perform this maintenance.
2. The exact source-tagged `platform-deploy.yml` preserves its existing release
   identity, authorization ticket, exact OSS evidence and WIF checks. Only for
   the fixed central service/repository/project/region and unchanged signed
   release profile, it invokes `prepare_quality_tooling.py` before the pinned
   engine. No new caller inputs or grants are introduced.
3. The helper reads the complete service template, operational annotations,
   labels, service account, resources, VPC/SQL settings, volumes, probes,
   environment/secret references and reconciled traffic. It never requests
   secret contents or prints provider responses. Duplicate JSON/env names,
   multiple containers, wrong writer, unreconciled/unready service or missing
   traffic fail before mutation. An already coordinated pair is a read-only
   success and does not create a second revision.
4. Update only both reviewed bundle pins in one `--update-env-vars` command
   with `--no-traffic`, without image, tag, IAM, ingress or resource flags.
   Never use `--set-env-vars` or `--env-vars-file`. The staging revision may
   retain the old controller image; it is never tagged, invoked or promoted.
   Re-read and compare every workload field, operational annotation/label,
   unrelated environment and secret reference, resolved revision percentages
   and tags. Only the two pins and generated revision/client metadata may
   change. LATEST is resolved to its previous concrete serving revision.
   Drift, partial update, timeout or provider failure stops the workflow before
   the engine. Provider errors are redacted and there is no automatic retry.
5. The existing no-traffic engine image update inherits both coordinated pins.
   Candidate smoke and the read-only source-tagged guard validate the exact
   resulting revision before promotion. Verify the serving controller image,
   both pins and manifest after promotion.
   Obtain fresh Smarti evidence under the new profile hashes and image digests.
   Previously eligible tags or green reports are not substitutes for mandatory
   UX6/PG41 results from those current identities.

No IAM policy is read or changed by the helper; its sole mutation cannot set
IAM/ingress flags. The full workload comparison covers security settings
exposed by the Cloud Run service, and existing ticket/WIF boundaries remain.

If preparation fails or the engine stops before promotion, the previous
serving revision retains its coherent image, manifest and pins. Reconcile the
template before another attempt; do not repeat an uncertain write blindly.
An update timeout can mean the mutation completed despite the failed command;
a timeout in the subsequent read means preservation was not established.
Both print only FAIL and abort before the engine, without retry or reversal.
Re-read the complete current template and traffic through the authorized
workflow/maintenance path and inspect the original deployment ID. Establish
whether both pins changed, whether configuration/traffic stayed intact, and
whether any execution is still active before deciding on another operation.
If preparation is abandoned, an authorized maintainer can restore both
template pins together with `--no-traffic` and verify unchanged traffic.
Historical rollback restores traffic to
the previous revision, whose image and pins remain coherent; it must not run
the new candidate-bundle gate on that historical revision. The service template
is not reverted by traffic rollback and must be reconciled before another
release. The engine still restores the exact previous traffic map on a failed
postpromotion smoke.

## Reversal

Stop the rollout if real-image checks or compatibility fail. Reverting an
activated contract requires explicit review of both manifest and executor
digests together, and fresh evidence for that reverted contract. Restoring the
old contract does not establish a pass for the new browser/PG requirements.
Never manufacture green evidence, reuse a mismatched hash, or bypass the gate.

## Recorded evidence and remaining application gates

For v0.32.0, API CI run
[37014645696](https://github.com/diegomad14/gcp-engineering-platform-api/actions/runs/37014645696)
published exact-commit oss-v2 PASSED: 1,219 tests, 85.89% coverage, Semgrep and
Trivy with no findings. Changed-line coverage was N/A (no modified executable
lines within existing roots), not a 100% result.

Official tooling run
[37018353170](https://github.com/diegomad14/gcp-engineering-platform-api/actions/runs/37018353170)
built and pushed both immutable quality images: python 45 image tests and node
52 image tests passed without skips; Playwright 1.62.1 installed Chromium and
headless shell revision 1234. The planner's nine tests also passed.

The six real Artemis browser/axe cases and mandatory remote Smarti PostgreSQL
execution still need fresh evidence. The earlier isolated 41-case PG17 result
and the image helper/unit tests do not establish that application CI pass.
Any subsequent guard/bundle commit likewise needs its own exact CI evidence.
