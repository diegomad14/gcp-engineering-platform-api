# Smarti mandatory CI: local candidate and coordinated rollout

This candidate is prepared against main `a3fb47a0c9c6855990557060ba0b0598698bcba6`.
It has not been published, activated, or built as an executor image. It does not
change FinOps, alert delivery, IAM, release permissions, or coverage thresholds.

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
   tests and adversarial supervisor tests, then launch the six real browser/axe
   cases as UID 65532 with the normal mounts/capabilities and no tokens. Also
   verify PG integration through the actual canonical and fallback containers.
4. Record the resulting immutable OCI digests and OS package inventory. The
   versioned Playwright installer selects dependencies; the reviewed OCI digest
   freezes resolved Debian packages. Do not reuse the old cached digest or an
   unversioned image tag. Any change to browser/package pins requires rebuild
   and renewed real-image evidence.
5. Coordinate the control-plane manifest and executor digests so both authorize
   exactly the same hash. A mismatch must fail closed. Trigger fresh quality
   execution on each reviewed compatible Smarti head and verify mandatory
   categories plus all existing gates before release review.

## Reversal

Stop the rollout if real-image checks or compatibility fail. Reverting an
activated contract requires explicit review of both manifest and executor
digests together, and fresh evidence for that reverted contract. Restoring the
old contract does not establish a pass for the new browser/PG requirements.
Never manufacture green evidence, reuse a mismatched hash, or bypass the gate.

## Evidence still required outside this local preparation

The six browser/axe cases, the new image build, image-root adversarial tests and
remote canonical/fallback execution have not been demonstrated here. Docker is
not installed in this cloud workspace. The earlier cloud Chromium restriction
is unchanged. Central Semgrep `--config auto` was not run because its metadata
egress was not approved for this repository; no suitable cached local Semgrep
rule set was found. Local unit tests, coverage, lint, types and the genuine
41-case checker against an independently created PG17 cluster are reported
separately and must not be described as a complete CI pass.
