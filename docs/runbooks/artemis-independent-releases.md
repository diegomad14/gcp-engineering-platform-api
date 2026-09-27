# Independent Artemis deployments

## Invariants

The twelve backend resources use Cloud Build exclusively. The catalog blocks an
incomplete executor, repository mapping or profile before creating a queued
request. Backend and pinned executor profile fingerprints must match. Existing
image reuse still requires exact repository and commit OCI labels and passed
oss-v2 evidence. Artemis web is a separate release.

`APP_RELEASE_SCOPE=runtime-v1` enables grants keyed by resource and commit.
`APP_RELEASE_RESOURCE` must match native `K_SERVICE` or `CLOUD_RUN_JOB`; worker
profiles must also match. A missing grant or unavailable store fails closed.
Legacy revisions keep using the old activation row during migration. Never
change that row while legacy runtimes remain in use.

Services stage without traffic, verify structured identity, grant the candidate,
verify readiness, then promote. Prior runtime grants drain for one hour. On
failure only the affected resource's traffic and grants are restored. Explicit
rollback refuses revisions without the independent protocol.

Jobs snapshot the full definition in the evidence bucket, pause only enabled
schedulers, and stage with both `--check-runtime` and
`APP_RUNTIME_CHECK_ONLY=true`. Dispatcher invocations during this interval also
perform checks only. After a successful check, original arguments are restored
and business execution is enabled. Recovery verifies the previous definition
before resuming schedulers. Failed recovery leaves triggers paused for operator
intervention. Paused export and Smarti schedules must remain paused.

## Release and migrate

1. Publish Engineering Platform API, web and the pinned release executor using
   the standard exact-commit oss-v2 release process. Preserve compatible platform
   profile hashes during the executor update.
2. Publish the Artemis compatible release. Update the management job
   `cgm-artemis-corporate-activate` to that verified immutable image, with command
   `python -m cgm_sanplat_param.security.runtime_rollout migrate`. Execute once.
   This creates only runtime grant/audit tables; it does not change legacy grants.
3. Verify the executor's Run update/invoke permissions, permission to act as every
   existing runtime identity, Scheduler pause/resume permissions, management-job
   execution with overrides, and evidence-bucket write permission. Keep workers
   private and the API's public OAuth/MCP access unchanged.
4. Review `scripts/configure_artemis_independent_releases.py` output. Apply routing
   after the executor supports the new profiles. Apply scheduler URI corrections
   only during migration of jobs; review paused states before/after.
5. Register verifiable historical direct deployments with
   `scripts/record_verified_artemis_runtime.py`. It checks tag, live revision,
   immutable image OCI provenance and exact quality evidence before appending a
   new `external_verified` event. Existing failed attempts remain unchanged.
6. Deploy the MCP worker first using the platform's tagged deployment command.
   Verify its `/ready` identity, commit and active reason. Test rollback to a
   compatible independently activated revision and confirm other resources are
   unaffected. Proceed resource by resource: task workers, dispatcher, API, jobs.
7. Check strict readiness on all services. For enabled jobs run one controlled
   execution, then observe a natural scheduler cycle. Record SanPlat errors
   separately from deployment/activation errors. Verify all five target URLs end
   in the literal `<job>:run` and no active scheduler returns 404.

## Recovery

Never authorize a different service under another resource's grant. Use the
management job only; the release executor has no database secret. Mutations are
transactional and idempotent, and require an actor and evidence URI. The managed
release command is the normal rollback interface. A failed job recovery requires
inspection of its stored pre-deployment definition before resuming paused
triggers. Do not reopen the global activation window as a workaround.

The panel distinguishes serving/configured revision, runtime commit, latest
deployment attempt, and last job execution. A failed historical attempt does not
by itself imply that the serving runtime is down.
