# SanPlat → Artemis multi-service migration

This rollout is deliberately gated. The old `cgm-sanplat-*` resources remain
authoritative until each new runtime has an independently verified deployment
and rollback. Never infer activity from the mere existence of a Cloud Run Job.

## Current status (2026-09-24 UTC)

- The existing GitHub repositories have been renamed in place: API ID
  `1306114845` is `diegomad14/cgm-artemis-api`; Web ID `1306114872` is
  `diegomad14/cgm-artemis-web`. History, tags and releases were preserved.
  Cloud Build v2 links for both new names exist and exact-ref discovery was
  verified. Historical records continue to resolve through the stable IDs and
  old-name aliases.
- Central PR #92 merged as `5d13f0d`. It raises Semgrep's per-rule timeout to
  30 seconds without excluding rules and adds a single-image `quality-python`
  publisher option. GitHub Actions published
  `quality-python@sha256:05a100de20959ccd8c5f3ad338ab8a1959e10ceb70cbb2d940d2c3fffdd0656d`.
  The exact Semgrep 1.136.0 binary scanned the API PR checkout with zero
  findings; existing GitHub Actions YAML `PartialParsing` warnings remain
  visible and are accepted by the trusted scanner.
- `eng-platform-api-semgrep30` now serves 100% traffic. It differs from the
  previous revision only in `ENG_PLATFORM_QUALITY_PYTHON_IMAGE`; `/health`
  returned 200 before and after promotion. The former revision
  `eng-platform-api-ep-a2157abce4` remains available at 0% for rollback.
- API PR #120 merged with head `85245215f29e100e188eae94b5922dcb2fa25be6`.
  Its Cloud Build PR-quality canary is
  `1739df8a-19ab-4082-8022-6365975d57c7`; it completed successfully and the
  backend published exact `oss-v2 PASSED` evidence for that PR head. Its merge
  commit is `c0c84f2566f822b30b5e4dcb6314e6a4402c28a7`; its main-release
  Cloud Build `e7c5be0d-1c1b-41c9-a717-389a7ba79535` completed successfully
  and the backend recorded exact `oss-v2 PASSED` evidence. The release is still
  in canary approval and no API tag was published. The plan has not yet been
  reviewed/approved, and no Artemis deployment was started.
  The preceding canary ran 1,190 tests (5 skipped), Ruff, compile and Trivy,
  but failed the full gate on the old Semgrep timeout and a workflow-pattern
  finding. The current PR head documents the verified credential isolation for
  that specific workflow finding.
- Central platform PR #94 merged as `bc06824515460896fdaa0ca296d66fded2c552aa`.
  Its profiles build Artemis Web with the release tag, disable embedded API
  background tasks, pin each isolated task-worker type, and let the dispatcher
  resolve only the four server-owned Artemis worker URLs. Unit and integration
  tests plus GitHub CI, normalized quality and workflow checks passed. This
  changes executor behavior but does not provision resources or enable any
  Artemis catalog entry.
- `gs://cgm-artemis-data/kpi_cgm.db` was refreshed from the authoritative
  SanPlat object. Source generation `1790283727562435` and target generation
  `1790286356815430` are both 76,107,776 bytes and have matching MD5
  `ad4q52gyOwkOiMfaROen3A==`. The prior target object was retained under
  `migration-baselines/2026-09-24/`. This is only a point-in-time copy, not
  write mirroring or cutover parity.
- No Artemis Cloud Run Service or Job exists. All 12 Artemis catalog entries
  remain `deployment.enabled=false` and not deployment-ready. The platform
  executor still updates existing Run resources and has no audited first-create
  path with exact candidate/rollback behavior. Do not enable descriptors,
  publish new runtime traffic, or route triggers to Artemis yet.
- A least-privilege preparation pass created 12 dedicated Artemis runtime
  service accounts and 26 `cgm-artemis-*` Secret Manager aliases. Secret bytes
  were copied directly in memory and each source/target pair was verified by
  SHA-256 without printing values. Accessor bindings are scoped to individual
  secrets and runtime identities; Cloud SQL client access is limited to the
  database-using runtimes. Only the API and readings-export identities have
  object-user access to `cgm-artemis-data`. The dispatcher can attach the
  dedicated `artemis-tasks-invoker` identity but cannot impersonate it project-
  wide.
- Four Artemis Cloud Tasks queues now exist with low dispatch limits and are
  all **paused and empty**: `cgm-artemis-sync`, `cgm-artemis-clock-sync`,
  `cgm-artemis-data-recovery`, and `cgm-artemis-fnd-ip-sync`. No Artemis
  schedulers exist. Production Run traffic, tasks, schedulers and the physical
  database remain SanPlat-authoritative. The queues must remain paused until
  their target Services, resource-level invoker policies, OIDC behavior,
  release-gate state and rollback path have all been verified.
- The SQL restore-mechanics test is complete and its temporary instance
  deleted; row-level production data parity remains outstanding. The API
  profile explicitly disables embedded background tasks on Artemis to prevent
  duplicate schedulers, but Perseo WM alerts, orphaned-report recovery, cache
  refresh and any other startup-managed work have not yet been relocated to
  independently owned triggers. This is a cutover blocker, not a safe default
  to ignore.

The following preparation snapshot is historical; statements about old GitHub
names and earlier bucket generations are superseded by the current status above.

## Historical preparation snapshot (2026-09-23 UTC)

- The two GitHub repositories still have their old names and immutable IDs
  `1306114845` (API) and `1306114872` (Web). Neither was renamed.
- WIF conditions for `cgm-github-oidc`, `cgm-release-oidc` and
  `cgm-sanplat-secrets-bootstrap/github-oidc` now accept both old and Artemis
  repository names. New `principalSet` grants were added to the three existing
  SanPlat service accounts; the old grants remain. Verify these before rename.
- Cloud SQL `cgm-sanplat-pg` is still the physical database. Its automated
  backups remain disabled. On-demand backup `1790181514872` completed at
  16:40 UTC. Restore operation `315d66ea-3e24-4ef7-89fa-d3b900000032`
  reached `DONE` on a separate `db-f1-micro` instance at 17:42 UTC; that
  instance was `RUNNABLE` and listed the same five databases as the source.
  The temporary instance was deleted by operation
  `038983b2-b0a4-4da9-a166-793800000032` (`DONE` at 17:48 UTC), and the
  source remained `RUNNABLE`. This checks restore mechanics and database
  inventory, not row-by-row equality. The backup remains available.
- The live API revision changed during preparation. At 16:36 UTC,
  `cgm-sanplat-api-hist-98e6369` began serving 100% traffic. Its revision name,
  `commit-sha` label (`03990b9…`) and `APP_RELEASE_SHA` (`a03fb62…`) disagree.
  Build `422675fb-62f7-415d-8fb4-bf109b67666d` produced digest
  `sha256:514265c…` from immutable source archive generation
  `1790181286322351`. All 236 regular files in that archive matched
  `origin/main` commit `98e63691c3318528614d3aa9983b1a0c443bd10a` byte
  for byte; `.gitignore` was the one tracked file excluded. This establishes
  source provenance while the label and env var remain stale drift. Do not
  use those metadata fields as the release authority.
- Artemis platform PR #62 merged as `da46f6a6` and API tag `v0.25.0` has
  exact `oss-v2 PASSED` evidence (84.17% changed-line coverage). Deployment
  `6620471675` completed through GitHub Actions without a Cloud Build and
  served revision `eng-platform-api-ep-b092dbc54b` at 100% traffic. Its
  digest `sha256:9cc6a47a…` equals the tag's Artifact Registry digest;
  `/health` was healthy. The revision inherited a stale `commit-sha` label;
  this was corrected in the subsequent deploy below.
- API PR #63 merged as `1aa8cba6`; tag `v0.25.1` has exact `oss-v2 PASSED`
  evidence. Its first deployment `6620957836` failed before creating a
  candidate because the newly published executor image could not import
  `yaml`. No product image or traffic was changed by that deployment. The
  executor image setting was restored to the previous digest in both GitHub
  and the API's live revision while the image was repaired.
- PR #64 merged as `b7e89d33`. Its release-executor Dockerfile pins
  `/usr/bin/python3` for the `apk`-installed YAML module, uses a parseable
  `USER root` directive, and has a narrowly path-scoped Trivy exception for
  the short-lived Cloud Build executor. The publisher ran an import and
  `gcloud` smoke *inside the built container before pushing* digest
  `sha256:8032c7d3…` from GitHub Actions; a local unprivileged import also
  passed. The dedicated GitHub tooling WIF pool/provider is restricted to
  this repository, workflow, `main`, manual dispatch and the operator ID;
  that pool can impersonate only the builder service account, whose Artifact
  Registry writer grant is repository-scoped.
- `eng-platform-api v0.25.2` has exact `oss-v2 PASSED` evidence for
  `b7e89d33`. Deployment `6621352620` succeeded through GitHub Actions run
  `35902320296` and serves `eng-platform-api-ep-c9d405912f` at 100%.
  Its image digest `sha256:7e271e64…` matches Artifact Registry, revision
  label `commit-sha=b7e89d33…` matches the tag, `/health` is healthy, and
  MCP `get_deployment` returned `SUCCEEDED` without a REST/browser refresh.
  An unauthenticated `/mcp` call still returns 401. All twelve Artemis
  descriptors remain `deployment.enabled=false`; no Artemis Cloud Run
  runtime, GitHub repo rename or public URL cut has occurred.
- Private regional bucket `cgm-artemis-data` was created with uniform access,
  public access prevention and seven-day soft delete. Initial copies of
  `kpi_cgm.db`, `location-snapshots/`, `readings-universe/` and
  `corporate-release/` completed. The 75,841,536-byte KPI object had matching
  MD5 (`mqaFOv+A2oL0xUGbqTga1g==`); checksum-only dry runs showed no
  differences across the three copied prefixes. `cloudbuild/` was not copied.
  This is a baseline copy only: new writes still go to the SanPlat bucket.
- Docker Artifact Registry `cgm-artemis-repo` was created in `us-central1`
  with immutable tags. Only the existing eng-platform release-executor service
  account was granted repository-level `artifactregistry.writer`; no Artemis
  image or Cloud Build was created for this migration.
- Source branches `codex/artemis-api` and `codex/artemis-web` remain on the
  original private repositories without PRs or deployments. Local API tests
  passed (`1108 passed, 79 skipped`) after installing the existing optional
  PostgreSQL test dependency; Web tests (`284 passed`) and production build
  passed. Private-repository quality/release orchestration is still disabled,
  so opening those PRs now would hit the unresolved GitHub Billing gate.
- Release-fallback PR #60 passed all GitHub checks, including normalized
  `oss-v2`, and merged to `main` as `a04421a3` at 17:27 UTC. The fix added
  release-path tests (80.36% changed-line coverage locally), removed the
  untrusted checkout from the credentialed portion of the PR workflow,
  set the planner image to a non-root default, and documented two narrowly
  scoped Trivy exceptions for the quality supervisors.

## Identity checks after changing GitHub names

1. Verify no deployment or release execution is active before changing release
   workflows or publishing a service tag.
2. Continue using stable repository IDs and alias-aware evidence lookup. Do not
   rewrite historical releases, deployments, fingerprints or audit records.
3. Recheck both-name WIF conditions, principal bindings, GitHub App installation
   and webhook delivery for the renamed repositories before promoting workflows.
4. Verify Cloud Build `fetchGitRefs` for the exact source SHA whenever a new
   connection is used. The rename itself is complete; do not create replacement
   GitHub repositories or revert names as part of runtime work.
5. The serving SanPlat API revision still has mismatched SHA metadata. Continue
   to rely on verified source provenance and image digest, not its stale label
   or `APP_RELEASE_SHA`, until a separately verified release replaces it.

## Runtime rollout

1. Build and pin the Artemis release executor once by digest. Keep all new
   services disabled until IAM, Artifact Registry and connected-repository
   mappings are ready. Automated tests must not submit Cloud Builds.
2. Create Artemis API/Web and worker runtimes in parallel using reviewed
   snapshots of current secret **references**, Cloud SQL attachment, bucket,
   service account, limits, ingress and authentication. Do not copy secret
   values into logs or source files. The physical SQL instance remains
   `cgm-sanplat-pg` behind the Artemis secret alias.
3. Copy operational Storage data to `cgm-artemis-data`, verify object
   generations/checksums, and keep a write-consistent rollback path. Do not
   retire `cgm-sanplat-data` during the overlap.
4. Route one Cloud Tasks worker type at a time from the dispatcher using the
   allowlisted per-type Artemis URL and queue. Drain the old queue first and
   preserve the durable lease. Never activate two dispatchers for one trigger.
5. Move scheduled Jobs one at a time. Snapshot the full Job definition before
   image update; do not run business work merely to test a deploy. Wait for
   active executions to finish before changing scheduler targets. Keep paused
   schedulers paused. Restore the definition and scheduler state on rollback;
   this does not reverse work already committed to SQL or Storage.
6. Cut API then Web URLs only after auth, CORS, proxy, callback, Perseo,
   Storage and health checks pass. Keep old URLs serving during the rollback
   window and expect users to sign in again on the new host.
7. After two successful cycles of every active scheduler and no unexplained
   old traffic, remove legacy hooks from the old API release workflow and
   schedule retirement of old resources separately. Do not delete pilots,
   historical Jobs, SQL or audit records as part of this cutover.

Every deploy from a shared API tag selects one service only. Backend-owned
Cloud Build policy, exact `oss-v2` evidence and service-specific locks remain
mandatory. A Job's public revision is the content hash of its saved definition;
the private snapshot is in the evidence bucket, not a Cloud Run traffic
revision.
