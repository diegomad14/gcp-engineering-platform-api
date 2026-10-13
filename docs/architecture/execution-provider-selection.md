# Execution provider selection

Release/CI and deployment are separate operations. Release runs the existing
sealed quality profile and semantic release planner, then publishes a tag. Deploy
consumes the exact passed release evidence and builds/promotes the selected
service. Rollback moves traffic to a previously recorded revision. Selecting a
provider never changes those gates or adds a second quality suite to deployment.

The trusted catalog can set `release.executor` to `github_actions` or
`cloud_build`, independently of `deployment.executor`. Missing release configuration
and deployment `auto` preserve the existing legacy policy. Existing production
catalog entries and environment defaults are unchanged by this patch.

For an individual operation, REST and MCP accept optional `provider` with exactly
`github_actions` or `cloud_build`. Omission inherits the service default, then the
legacy policy. `auto`, runner selection and arbitrary executors are not request
options. An explicit request overrides a conflicting default. Readiness and
authorization are still required for the chosen provider.

```
POST /api/services/{service}/deployments
Idempotency-Key: <stable operation key>
{"tag":"v1.2.3","provider":"github_actions"}

POST /api/services/{service}/deployments/{target}/rollback
Idempotency-Key: <stable operation key>
{"provider":"github_actions"}

POST /api/services/{service}/release-executions
{"head_sha":"<40 hex>","base_sha":"<40 hex>","provider":"github_actions"}
```

Release identity is its immutable repository/service/SHAs/profile/policy/planner
fingerprint. Manual release requires the current main SHA and an ancestor base.
The API uses the canonical `eng-platform-release.yml` workflow, with an execution
ID checked before engine startup. OIDC verification binds repository, main ref,
SHA, workflow, event and run. A concurrent native push cannot admit a second run.

New records retain `provider_selection.requested`, `service_default`, `effective`,
`source` and `locked`. Defaults changing later do not rewrite these fields.
An existing request asking for another provider returns 409 before dispatch.
Legacy records keep their persisted provider. A GitHub Deployment object (and
even a stale run reference) cannot identify it: Cloud Build also creates that
object. Unidentified records require reconciliation and return 409 on retry.
The UI labels records without selection metadata as legacy rather than presenting
today's default as historical intent.

GitHub billing, quota, permission, YAML and generic failures stop the selected
operation. Reads and workflow completion notifications never submit a replacement
Cloud Build. A paused native Cloud Build webhook is rejected before reserving
new identity, so a later explicit GitHub request for that SHA remains possible.
New locked execution records reject provider transitions. Known
dispatch rejections can retry the same provider; uncertain submission, accepted
dispatch and an in-progress dispatch require investigation before retry. The UI
keeps the same idempotency key while its dialog remains open.

Rollback identity includes the original target deployment ID and revision,
even when two targets share a tag. Repository records must agree on one
CI/release default; deployment defaults remain independent per service.

Deployment admission is durable before creating any external object. The key is
bound to its GitHub object before dispatch, so a failed post-dispatch store write
cannot permit another build. Incomplete admissions remain closed for explicit
reconciliation. Local snapshots use atomic replacement.

Local snapshot read/modify/write also holds a store-wide interprocess lock, so
commands for different services cannot overwrite each other's durable intent.
The lock covers persistence only, independently of per-service command fences.
GitHub retry claims are restored to their previous state after a lookup failure
only when workflow dispatch has not been attempted; uncertain dispatches remain
closed for reconciliation.

Artemis GitHub deploy/rollback additionally sends an `execution_config` JSON
snapshot with exactly seven server-owned fields: evidence bucket, runtime kind,
private runtime, Dockerfile path, quality evidence owners, repository aliases and
paired web SHA. It is bound atomically to the execution and hashed in the signed
ticket. Workflows validate its shape, verify the ticket, compare that signed hash
and only then export the bounded engine context. Retries reuse the first snapshot.
Other services keep their existing workflow dispatch inputs.

`ENG_PLATFORM_DEPLOYMENT_EVIDENCE_BUCKET` configures this storage independently
of Cloud Build admission; it can reuse the existing quality bucket or legacy
configured evidence bucket. Missing/invalid storage blocks new Artemis GitHub
admission. `cgm-artemis-api` also requires the exact paired web SHA. No bucket,
credentials, IAM or productive variables are provisioned by this change.

The prepared Artemis workflows use the existing pinned central engine, including
its independent runtime and Job branches, instead of the legacy repository
multi-job controller. They preserve candidate/production environment gates and
do not rerun quality during deployment. Existing tags whose workflow does not
declare `profile_sha256` cannot be dispatched with this new contract. Activation
requires separately approved reviewed source, fresh qualified tags and matching
executor digest/WIF/configuration; tags are never rewritten. GitHub capacity and
cost have not been established by the local checks.

Deployment command fences serialize validation and dispatch per service and key in
Firestore, or across local processes with flock. A crashed Firestore owner has no
automatic lease expiry: clear it only after proving whether its external dispatch
exists. GitHub admission and Cloud Build's existing submission claim independently
prevent two executions of one reservation.

`GET /api/services/{service}/release-executions` reads persisted status only.
`POST /api/services/{service}/release-executions/{id}/cancel` closes pending intent
or cancels an already bound provider run. It never submits replacement work.
Deployment history refresh can project provider state, but cannot launch a build.

## Activation after a separate approval

Operator decision, 2026-10-13: Artemis API/web promotion is automatic after all
mandatory gates and candidate smoke pass, under the authorized deployment.
Required reviewers are not a prerequisite for this promotion. This decision does
not authorize publishing or deploying the platform/engine, changing application
flags, or changing productive defaults. The only approved configuration exception
is `ENG_PLATFORM_CI_EXECUTOR=github_actions` in the two Artemis repositories, after
the active backend is verified to select GitHub without launching Cloud Build.
The latest Cloud Run evidence shows that condition is not yet met.

1. Review the local diffs, source hashes and validation report. Resolve every
   blocked normalized gate before publication. No changes have been pushed or
   dispatched by this task.
2. Publish API/web and the prepared workflow changes through the existing GitHub
   process after explicit approval. Reuse the evidence bucket, GitHub App and WIF
   identities. A release engine must implement the reviewed contract and remain
   pinned by digest. Staged Artemis deploys verify its code hash without invoking
   its entrypoint; an older engine cannot silently perform the complete deploy.
   Publishing a compatible engine through GitHub requires separate approval.
3. Verify strict GitHub selection for Artemis and the effective Cloud Build
   admission policy before activation. `ENG_PLATFORM_CLOUD_BUILD_NEW_BUILDS_ENABLED=false`
   is the new setting's default during the cost pause. Both deployment and release transports,
   bootstrap admission, provider transitions and planner retries enforce it before
   creating work. `ENG_PLATFORM_CLOUD_BUILD_ENABLED=false` also blocks creation.
   Publishing this backend must not silently change another service's effective
   admission policy. Global mode, admission flags and shared image pins require
   separate review; they are not part of the two repository-variable exception.
   Existing Cloud Build inspection and reconciliation remain available; they
   do not grant admission to another build. No productive setting is changed by
   this task. GitHub-only orchestration now starts without Cloud Build
   accounts, connected repositories or a Cloud Build evidence bucket. It retains
   the normal GitHub, quality evidence, pinned engine and reconciler requirements.
4. Verify per-service release/deploy defaults and the matching repository CI hints.
   Any catalog change requires its own authorization. Native push/PR workflows
   still use their existing repository
   variable guards, so those hints must agree with automatic release defaults.
   Manual GitHub releases are bound to explicit workflow-dispatch reservations
   and can bypass an old Cloud Build hint without changing that variable.
5. Audit legacy in-progress operations by reading their persisted stores; do not
   use a deployed old history endpoint that can refresh into fallback. Preserve
   their IDs and providers. Resolve uncertain dispatches before any retry.
6. After an approved cost budget, run one GitHub operation, verify exact SHA,
   normalized `oss-v2` evidence, logs and single execution, then approve rollout
   service by service. Do not use a billing probe or a Cloud Build fallback for
   activation. A later explicit Cloud Build choice requires its own approval
   and enabled prerequisites.

Current production SHA, live billing capacity, permissions, WIF readiness and
deployment state are not established by these local validations.

Artemis API and web deploys consume their signed, 300-second ticket before the
environment-bound execution jobs. The candidate job permits one build and
zero-traffic candidate validation, including the private runtime boundary. Only
a successful candidate job admits automatic promotion in the production job,
after all mandatory gates and smoke have passed. Promotion
downloads that same run's immutable artifact ID, verifies the candidate checksum
and live revision/image/configuration/traffic, and promotes without rebuilding.
Runtime activation grants occur only after production-stage authorization and
revalidation. The engine restores
the original traffic if production validation fails.

Each continuation requires a fresh GitHub OIDC identity for
the expected environment, matching the repository, workflow/ref/SHA, run and
attempt already recorded by the durable, one-time ticket consumption. The API
rechecks the signed operation/configuration/rollback target, current operator or
MCP grant and revocation state. Expiration still blocks an initial consumption;
an expired unconsumed ticket, another run/attempt or revoked authority cannot
continue. This does not extend the ticket TTL. If an existing environment rule
causes a wait, the same durable continuation checks apply afterward. An
environment claim identifies a job context; it does not prove human approval.
No new approval endpoint, confirmation view, second workflow run or ticket is
required by the automatic-promotion decision. Workers retain their existing full
engine operation in production; rollbacks consume before entering that stage and
revalidate the same authorized run afterward. This decision does not authorize
new worker or rollback operations.


The local change is reconciled with PR156 (`aee2bdd561f8f201743cde56dab5d7fc6a930e0d`). Its ACL/cache changes and `platform-github-only.yml` are preserved. That workflow changes productive enrollment and traffic; this task did not invoke it or verify its production execution. Treat any later use as a separately approved activation. Provider readiness in the selector is computed from local catalog/configuration and does not launch or probe either executor.
