# SanPlat → Artemis multi-service migration

This rollout is deliberately gated. The old `cgm-sanplat-*` resources remain
authoritative until each new runtime has an independently verified deployment
and rollback. Never infer activity from the mere existence of a Cloud Run Job.

## Current preparation (2026-09-23 UTC)

- The two GitHub repositories still have their old names and immutable IDs
  `1306114845` (API) and `1306114872` (Web). Neither was renamed.
- WIF conditions for `cgm-github-oidc`, `cgm-release-oidc` and
  `cgm-sanplat-secrets-bootstrap/github-oidc` now accept both old and Artemis
  repository names. New `principalSet` grants were added to the three existing
  SanPlat service accounts; the old grants remain. Verify these before rename.
- Cloud SQL `cgm-sanplat-pg` is still the physical database. Its automated
  backups remain disabled. On-demand backup `1790181514872` completed
  successfully at 16:40 UTC; a restore test has **not** been performed.
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
- Artemis catalog descriptors are staged with `deployment.enabled=false`.
  No Artemis Cloud Run resources, GitHub renames or URL changes were made.
- Private regional bucket `cgm-artemis-data` was created with uniform access,
  public access prevention and seven-day soft delete. Initial copies of
  `kpi_cgm.db`, `location-snapshots/`, `readings-universe/` and
  `corporate-release/` completed. The 75,841,536-byte KPI object had matching
  MD5 (`mqaFOv+A2oL0xUGbqTga1g==`); checksum-only dry runs showed no
  differences across the three copied prefixes. `cloudbuild/` was not copied.
  This is a baseline copy only: new writes still go to the SanPlat bucket.
- Docker Artifact Registry `cgm-artemis-repo` was created in `us-central1`
  with immutable tags. Only the existing eng-platform release-executor service
  account was granted repository-level `artifactregistry.writer`; no images
  or Cloud Builds were created for this migration.
- The backend branch depends on release-fallback PR #60. That PR is still
  open and behind `main`; its normalized `oss-v2` gate failed with three SAST
  findings, three Dockerfile misconfigurations and 72.43% changed-line
  coverage against an 80% minimum. Do not merge or bypass this gate for the
  Artemis rollout. The Artemis work is on `codex/artemis-platform`, based on
  that branch, and is not deployed.

## Gates before changing GitHub names

1. Verify no deployment or release execution is active in either repository
   and freeze the old release workflows for the rename window.
2. Reconfirm the serving API digest still points to the verified source
   above, correct the mismatched SHA metadata in a future controlled release,
   and capture its definition, traffic, digest and
   rollback procedure. Confirm the latest SQL backup is restorable on an
   isolated target; a successful backup operation alone is insufficient.
3. Deploy the repository-ID alias code and callback checks to eng-platform.
   Verify historical evidence and retries by old and new repository names.
4. Recheck WIF conditions and service-account bindings for both names, then
   rename the **existing** API repo and verify GitHub ID, tags, Releases,
   installation and webhook. Create the new Cloud Build 2nd-gen source link and
   verify `fetchGitRefs` for exact SHA before switching backend mapping.
5. Repeat for Web. If WIF or source link fails, restore the previous repository
   name before migrating any runtime; never create a replacement repo.

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
