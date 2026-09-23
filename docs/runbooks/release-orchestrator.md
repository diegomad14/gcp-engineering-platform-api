# Release orchestrator operations

The release orchestrator gives pull requests and `main` the same quality and
release semantics whether GitHub Actions or Cloud Build executes the work.
GitHub Actions is preferred while healthy. Cloud Build is a persistent fallback
for private repositories when GitHub conclusively cannot start work because of
Billing or included-minute exhaustion. Provider selection is internal and is
not present in public REST, MCP or frontend contracts.

The orchestrator prepares releases only. It does not deploy automatically.
After a successful `main_release`, an operator can deploy the eligible semantic
tag through the existing UI, REST or MCP command service. That deployment uses
the same backend selector and reuses the exact `oss-v2` evidence.

## Event and state model

The GitHub App sends three event types to
`POST /api/internal/github/events`:

- `pull_request` for `opened`, `reopened` and `synchronize`: reserve
  `pr_quality` with the exact head and base SHA. A newer head cancels only a
  superseded Cloud Build PR execution.
- `push` to `refs/heads/main`: reserve `main_release` with `after` as head and
  `before` as base. A zero base, identical SHAs, an unverifiable base or a
  non-fast-forward push is rejected.
- completed `workflow_run`: bind the exact workflow, event, SHA and run ID,
  reconcile GitHub results, classify a strict Billing rejection, and process a
  requested health probe.

`release_executions` is separate from `deployment_executions`. The stable
execution ID is a SHA-256 fingerprint over repository, service, operation,
head/base SHAs, quality profile, policy, executor digest and—on `main`—planner
hash. The intent is reserved before a dispatch. A replay with the same identity
resumes missing idempotent effects; reuse of that fingerprint with different
immutable identity is rejected before a build starts.

The provider may report a provisional quality report or release plan, but the
API does not commit it until the exact GitHub run or Cloud Build is terminal in
the expected way. Callbacks are monotonic: a duplicate or lower sequence is a
no-op. A different report for an already reserved fingerprint is an immutable
evidence conflict and leaves the execution `UNKNOWN` for investigation.

PR success ends at `quality_passed`; it is no longer due for reconciliation.
`main_release` repeats quality for the merge SHA, then follows one of these
paths:

- semantic changes: `release_planned`, then idempotent tag and GitHub Release
  publication by the API;
- no semantic changes: terminal `no_release` and a neutral release check;
- invalid evidence, provider failure or identity mismatch: no publication;
- uncertain publication: reconciliation first checks the tag and Release,
  completes a correct partial publication, and never moves a conflicting tag.

The API publishes these stable GitHub App checks, independent of provider:

- `Engineering Platform / quality`
- `Engineering Platform / workflows`
- `Engineering Platform / conventional-title`
- `Engineering Platform / release`

Once the canary is accepted, make these App checks the required checks on the
public API repository while preserving strict mode, administrator protection
and conversation resolution. GitHub Free private repositories may not enforce
the same branch protection; exact evidence remains the authoritative backend
gate for tags and deployments.

## Persistent Billing circuit

The circuit is scoped to `ENG_PLATFORM_GITHUB_BILLING_OWNER`, stored in the
`executor_circuits` Firestore collection and shared by that owner's private
catalog repositories. It survives API restarts and UTC month changes. Public
repositories always stay on GitHub Actions.

It opens only when evidence is conclusive:

- the GitHub billing API result, cached for five minutes within the current UTC
  month and summed only for private Linux repositories, is at or above
  `ENG_PLATFORM_GITHUB_INCLUDED_PRIVATE_MINUTES`;
- a matching workflow dispatch returns an explicit Billing, quota, included
  minutes, payment-required or spending-limit error; or
- a matching completed workflow has only zero-step Billing annotations, or is
  `startup_failure` with no jobs and a fresh quota lookup confirms exhaustion.

Tests, configuration errors, permissions, normal timeouts, cancellation and
generic GitHub unavailability do not open it. When open, the API writes the
server-owned repository variable (by default `ENG_PLATFORM_CI_EXECUTOR`) as
`cloud_build` for private repositories. Normal workflows inspect that variable
before requesting a runner. The health workflow is intentionally exempt.
Firestore is authoritative if a variable update is temporarily incomplete.
If the billing lookup is missing or fails, the selector attempts GitHub and
waits for the strict reactive evidence above; an API error alone never opens
the circuit.

The circuit never closes because time passed or a new billing month began. A
deployer must request a real, minimal GitHub Actions health probe. See
[Close the circuit](#close-the-circuit-with-a-manual-health-probe).

## GitHub App webhook

Configure the installed GitHub App with:

- URL: `<ENG_PLATFORM_API_URL>/api/internal/github/events`
- content type: `application/json`
- a random high-entropy secret stored in Secret Manager and exposed only to the
  API as `ENG_PLATFORM_GITHUB_WEBHOOK_SECRET`
- events: Pull requests, Pushes and Workflow runs
- repository access limited to the six managed repositories

The API accepts only HMAC-SHA256 `X-Hub-Signature-256`, the configured GitHub
installation ID, a managed catalog repository and one of those three event
types. Bodies over 2 MB are rejected. `X-GitHub-Delivery` is journaled in
`github_webhook_deliveries`; redelivery is idempotent only when event,
repository and payload hash are identical. Do not log the webhook secret or
full payloads.

The App permissions required by this feature are Actions read/write, Checks
write, Contents write, Deployments write, Metadata read and Pull requests read.
The organization/repository owner must approve the permission expansion before
cutover. Release quality builds never receive Contents write: Cloud Build gets
only a one-time installation token restricted to the source repository and
`contents:read`.

## Callback identity

All operational endpoints below are excluded from OpenAPI.

Cloud Build calls
`POST /api/internal/release-executions/{execution_id}/events` and the one-time
`.../{execution_id}/source-token` endpoint with Google OIDC. The token must:

- have audience exactly equal to the base `ENG_PLATFORM_API_URL` (no endpoint
  suffix);
- identify exactly `ENG_PLATFORM_RELEASE_CALLBACK_SERVICE_ACCOUNT`;
- correlate to the saved build ID.

The API then reads the real Cloud Build and checks the service account plus all
identity substitutions: execution, fingerprint, service, repository, head,
base, operation, profile hash and executor digest. A callback cannot select a
command, provider, profile or source SHA.

Before repository-owned code runs, the trusted preparation step also claims
one execution-scoped callback secret from
`.../{execution_id}/event-token`. Only its SHA-256 hash is persisted. The
plaintext is stored in a root-only control volume that is never mounted in a
repository-code step; every engine event needs both the provider OIDC identity
and this secret. Source-token and event-token issuance are each single-claim,
so repository code cannot use a copied metadata identity to mint another one.
Quality output, trusted external smoke output and control data use separate
volumes. The external result is root-owned, read-only and validated for exact
owner/mode before it is included in evidence.

GitHub Actions uses `id-token: write` and audience
`engineering-platform-release-orchestrator`. The API verifies issuer,
repository, exact workflow path on `main`, event, ref/SHA and run ID before
returning an authorized execution or accepting an event. No service-account
JSON key is used in either path.

## Images and server-owned profiles

Build and scan these images in their own controlled image pipeline, then copy
the resolved Artifact Registry digest into configuration:

```text
ENG_PLATFORM_QUALITY_NODE_IMAGE=us-central1-docker.pkg.dev/...@sha256:...
ENG_PLATFORM_QUALITY_PYTHON_IMAGE=us-central1-docker.pkg.dev/...@sha256:...
ENG_PLATFORM_RELEASE_PLANNER_IMAGE=us-central1-docker.pkg.dev/...@sha256:...
ENG_PLATFORM_RELEASE_POSTGRES_IMAGE=...@sha256:...
```

The API refuses to start with release orchestration enabled if any value lacks
`@sha256:`. Node quality serves `eng-platform-web` and `cgm-sanplat-web`;
Python quality serves the other four profiles. The planner runs only for
`main_release`. Commands, working directories, coverage thresholds, auxiliary
checks and 1,800/3,600-second timeouts come from the server-owned profile file,
never a webhook or client request. Do not rebuild any of these images per PR or
release.

The approved baseline intentionally standardizes quality on Node 22 and Python
3.12 even where an older workflow used Node 20 or Python 3.11. Treat the first
genuine canary of each such service as a runtime migration check. For
`cgm-bot-api`, the pinned `oss-v2` profile replaces the legacy secret-bearing
SonarQube job with blocking Semgrep, Trivy and Bandit plus dependency checks.
This is a deliberate policy migration: do not enable that service until the
security owner accepts the recorded profile hash. Never put a Sonar token into
a build that executes pull-request code.

`ENG_PLATFORM_RELEASE_EXECUTOR_IMAGE` is a fourth, separately pinned image for
deploy/rollback. Keeping it separate prevents quality images from acquiring
production deployment permissions.

## Feature flags and rollout modes

The API feature flags are intentionally fail-closed:

```text
ENG_PLATFORM_RELEASE_ORCHESTRATOR_ENABLED=false
ENG_PLATFORM_RELEASE_CANARY_SERVICES=
ENG_PLATFORM_RELEASE_ENABLED_SERVICES=
```

The first name exists in two control surfaces: it is an API runtime environment
variable and a GitHub Actions repository variable read by each thin workflow.
Enable the repository variable only after the API environment and webhook are
ready. Initialize `ENG_PLATFORM_CI_EXECUTOR=github_actions` in each private
repository; thereafter only the backend changes it with the circuit.

- With the master flag `false`, the GitHub webhook returns 404 and no release
  execution is created.
- A service in `ENG_PLATFORM_RELEASE_CANARY_SERVICES` runs quality and creates a
  release plan, but the API does not publish until a deployer approves that
  exact execution through
  `POST /api/internal/release-operations/canaries/{execution_id}/approve`.
- A service in `ENG_PLATFORM_RELEASE_ENABLED_SERVICES` is in auto publication:
  an exact successful `main_release` may publish its planned tag and Release.
- The two service lists must be disjoint; configuration validation rejects an
  overlap.

For deployment fallback, independently configure:

```text
ENG_PLATFORM_CLOUD_BUILD_ENABLED=true
ENG_PLATFORM_DEPLOY_EXECUTOR_MODE=auto
ENG_PLATFORM_CLOUD_BUILD_ENABLED_SERVICES=<allowlist>
```

`auto` is a backend policy, not a user option. `cloud_build` and
`github_actions` remain available only as internal emergency configuration
modes. Keep production on `auto` after rollout.

Other required release settings are:

```text
ENG_PLATFORM_RELEASE_EXECUTION_FIRESTORE_COLLECTION=release_executions
ENG_PLATFORM_EXECUTOR_CIRCUIT_FIRESTORE_COLLECTION=executor_circuits
ENG_PLATFORM_GITHUB_WEBHOOK_DELIVERY_FIRESTORE_COLLECTION=github_webhook_deliveries
ENG_PLATFORM_GITHUB_WEBHOOK_SECRET=<Secret Manager value>
ENG_PLATFORM_RELEASE_QUALITY_SERVICE_ACCOUNT=projects/PROJECT/serviceAccounts/<quality-build SA email>
ENG_PLATFORM_RELEASE_CALLBACK_SERVICE_ACCOUNT=<same quality-build SA email>
ENG_PLATFORM_RELEASE_RECONCILER_SERVICE_ACCOUNT=<dedicated scheduler SA email>
ENG_PLATFORM_RELEASE_POSTGRES_IMAGE=<postgres image>@sha256:<digest>
ENG_PLATFORM_GITHUB_MODE_VARIABLE=ENG_PLATFORM_CI_EXECUTOR
ENG_PLATFORM_GITHUB_HEALTH_REPOSITORY=diegomad14/gcp-engineering-platform-web
ENG_PLATFORM_GITHUB_HEALTH_WORKFLOW=eng-platform-actions-health.yml
ENG_PLATFORM_CLOUD_BUILD_MINUTE_PRICE_USD=<current applicable rate>
ENG_PLATFORM_CLOUD_BUILD_USAGE_ALERT_MINUTES=2000,2250,2500
```

`ENG_PLATFORM_QUALITY_BUCKET` stores immutable `oss-v2` evidence and must be
configured. It may differ from `ENG_PLATFORM_CLOUD_BUILD_EVIDENCE_BUCKET`, which
holds deployment summaries; do not repoint either existing bucket during
activation or historical reconciliation can lose its source. Apply 30-day
retention only to transient `quality/pending/release-executions/` and
`quality/summaries/release-executions/` objects, never to the evidence prefix.

The release executor obtains its Google OIDC token from the Cloud Build metadata
server as the service account attached to the build. Therefore the callback
email must identify the same account as the full Cloud Build service-account
resource. The same rule applies to deployment. Configuration normalizes these
two representations and fails closed when their subjects differ. A distinct
callback identity requires a future, explicit impersonation implementation;
merely changing the environment variable will make callbacks fail
authentication.

Cloud Build repository steps can reach the attached build identity through the
metadata server. Keep the quality service account deliberately low-value:
Artifact Registry reader for the pinned tooling images, Cloud Logging writer
and API invoker only. It must not read Secret Manager, write Artifact
Registry/GCS, create builds, deploy Cloud Run, publish GitHub content or access
production resources. Dual OIDC plus the root-only event token protects the
control plane, but it is not a substitute for this least-privilege boundary.

## Reconciliation scheduler

Create one regional Cloud Scheduler HTTP job that calls
`POST <ENG_PLATFORM_API_URL>/api/internal/release-operations/reconcile` every
two minutes. Its OIDC service account must be exactly the dedicated
`ENG_PLATFORM_RELEASE_RECONCILER_SERVICE_ACCOUNT`, must differ from the quality
build/callback identity, and its audience must be exactly the base
`ENG_PLATFORM_API_URL`. That account needs Cloud Run Invoker on the API service
and no build or release permissions.

An equivalent command is:

```bash
gcloud scheduler jobs create http eng-platform-release-reconcile \
  --location=us-central1 \
  --schedule='*/2 * * * *' \
  --time-zone=UTC \
  --uri="${ENG_PLATFORM_API_URL}/api/internal/release-operations/reconcile" \
  --http-method=POST \
  --oidc-service-account-email="${ENG_PLATFORM_RELEASE_RECONCILER_SERVICE_ACCOUNT}" \
  --oidc-token-audience="${ENG_PLATFORM_API_URL}"
```

The endpoint processes at most 100 oldest due records per invocation. Terminal
executions and successful PR quality are excluded. Reconciliation is safe to
repeat: it verifies provider state before committing evidence, completes
partial tag/Release publication, and reconciles an uncertain Cloud Build submit
before considering its single bounded retry. Alert if the scheduler fails, the
due set grows continuously, or any execution remains `unknown`.

## Close the circuit with a manual health probe

Do not schedule probes. They intentionally consume a small amount of private
GitHub Actions quota and exist only for an administrator who has already fixed
Billing. Confirm the configured health repository is private and contains
`.github/workflows/eng-platform-actions-health.yml`, then as an authenticated
allowlisted deployer call:

```text
POST /api/internal/release-operations/github-actions/probe
```

The API records a random nonce before dispatching the workflow. A later
`workflow_run` must match the exact repository, workflow, `workflow_dispatch`
event and nonce-bound display title. The API also re-reads jobs from GitHub.
Only `success` with at least one job that actually started can restore all
private repository variables to `github_actions` and close the circuit. A
failed, zero-job, unrelated or forged run leaves it open. If variable updates
are only partially successful, the API rolls them back to `cloud_build` and
keeps the circuit open; fix the cause and request a new probe.

## Exact production rollout

This rollout authorizes one real release canary for `eng-platform-web`, one
manual deploy of its result and one rollback. No synthetic build is run for the
other five services.

1. **Prepare without execution.** Install the GitHub App permissions and HMAC
   webhook, connect all six v2 repositories, publish the four digest-pinned
   images, create least-privilege identities, configure the private Firestore
   collections, apply 30-day GCS lifecycle rules and create the reconciliation
   scheduler. Install the thin PR/`main` workflows in every managed repository
   and the one-step health workflow in the configured private health
   repository. Keep the API and repository
   `ENG_PLATFORM_RELEASE_ORCHESTRATOR_ENABLED` flags false, keep Cloud Build
   disabled and keep both service allowlists empty.
2. **Deploy the API dark.** Release the API from its public repository with
   `ENG_PLATFORM_RELEASE_ORCHESTRATOR_ENABLED=false` and
   `ENG_PLATFORM_CLOUD_BUILD_ENABLED=false`. Verify `/health`, webhook 404,
   scheduler authentication and read-only access to the required GCP resources.
   A scheduler authentication check must not create a release execution.
3. **Enable only the web canary.** Set
   `ENG_PLATFORM_RELEASE_CANARY_SERVICES=eng-platform-web`, keep
   `ENG_PLATFORM_RELEASE_ENABLED_SERVICES` empty, configure all required image
   digests and identities, and then set
   `ENG_PLATFORM_RELEASE_ORCHESTRATOR_ENABLED=true` on the API. After the API
   is healthy, set the same GitHub repository variable to `true` for the web
   repository. Enable Cloud Build with
   `ENG_PLATFORM_CLOUD_BUILD_ENABLED_SERVICES=eng-platform-web` and keep
   `ENG_PLATFORM_DEPLOY_EXECUTOR_MODE=auto`.
4. **Recover the known Billing failure once.** Prefer the signed
   `workflow_run` webhook. If that historical delivery was missed, an
   authenticated deployer may call
   `POST /api/internal/release-operations/reconcile-github-run` with service
   `eng-platform-web`, operation `main_release`, the full 40-character head and
   base SHAs and the exact GitHub run ID. For the planned migration, verify the
   head begins `4b3a173`, the base begins `8a6c13d`, the run is the expected
   `main` release workflow and the calculated plan is `v0.17.5`. Never submit
   the abbreviated prefixes. The API must independently confirm the run is the
   same zero-step Billing rejection before it opens the circuit and submits the
   single real release-quality Cloud Build.
5. **Accept the release canary.** Verify the Cloud Build used
   `E2_STANDARD_2`, `CLOUD_LOGGING_ONLY`, the exact connected-repository SHA,
   the quality Node and planner digests, the dedicated quality service account
   and no unexpected retry. Verify exact `oss-v2` evidence, all four stable
   checks, timing/cost fields and the `v0.17.5` plan. Approve that execution via
   `POST /api/internal/release-operations/canaries/{execution_id}/approve`.
   Approval publishes the existing plan and must not launch another build.
6. **Deploy once and roll back once.** Start the `eng-platform-web` deployment
   for `v0.17.5` through UI, REST or MCP. With the private circuit open, the
   backend selects Cloud Build without exposing it. Confirm the deploy consumes
   the canary evidence and performs at most one image push. Then request a
   rollback to the immediately previous successful deployment through the same
   public command surface. Verify exact prior revision traffic is restored and
   no image, quality suite or planner is rebuilt. Do not redeploy merely to
   generate more test evidence.
7. **Promote configuration, not synthetic work.** Remove
   `eng-platform-web` from `ENG_PLATFORM_RELEASE_CANARY_SERVICES`. Put all six
   catalog services in `ENG_PLATFORM_RELEASE_ENABLED_SERVICES` and
   `ENG_PLATFORM_CLOUD_BUILD_ENABLED_SERVICES`, keeping the two release lists
   disjoint. Keep the deployment selector on `auto`; the release enabled list
   is the auto-publication allowlist. Set the release-orchestrator repository
   variable to `true` in the remaining five repositories. Do not replay old
   PRs or create empty commits. Each remaining profile is exercised only by its
   next genuine PR or `main` change.
8. **Cut over checks and publishers.** After the web execution and rollback
   audit are clean, migrate the public API branch protection to the four App
   checks. Remove the old semantic-release publisher workflows only after the
   replacement is observed stable; never leave two publishers active.

Expected real Cloud Build consumption for this validation is bounded to one
web `main_release` build, one web deploy execution and one web rollback
execution. The approval step, reconciliation, check updates and publication do
not create another build. Stop before the next step if identity, evidence,
traffic or publication is `UNKNOWN`.

## Local validation and safety

All automated unit, contract and integration tests use mocks, fake provider
responses and in-memory/fake Firestore or storage. They may inspect generated
Cloud Build requests and reconcile recorded snapshots, but must never call the
Cloud Build API, dispatch a GitHub workflow, mutate repository variables,
publish tags/Releases, deploy or move traffic. A passing local test suite is not
authorization for a cloud canary. The explicit rollout step above is the only
real canary; it must be initiated and observed by an operator.

For a missed, already completed GitHub run, use the authenticated
`reconcile-github-run` operation rather than fabricating a webhook. For any
`UNKNOWN` execution, preserve its Firestore record and build/run IDs, pause new
work for that service, and let the scheduler or an operator reconcile existing
effects before considering another submission.
