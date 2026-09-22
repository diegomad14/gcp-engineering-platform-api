# Cloud Build Economy

Cloud Build Economy is the internal fallback for both release preparation and
deployment. GitHub Actions remains preferred. When private Actions minutes are
conclusively exhausted, or GitHub rejects a matching run for Billing, quota,
payment or spending-limit reasons, Engineering Platform opens a persistent
account circuit and sends subsequent private-repository work to Cloud Build.
The executor is never selectable through the UI, REST or MCP contracts.

There are two deliberately separate execution planes:

| Plane | Operations | Images | Authority |
|---|---|---|---|
| Release orchestration | PR quality and `main` quality/release planning | Node quality, Python quality and release planner | Produces immutable `oss-v2` evidence; the API alone publishes tags and GitHub Releases |
| Deployment | Deploy and rollback of an already authorized tag | Release executor | Builds at most one service image for a deploy; rollback never rebuilds |

See [Release orchestrator](release-orchestrator.md) for the circuit, webhooks,
checks, callbacks, reconciliation and rollout procedure.

## Economy contract

Every generated Cloud Build request is server-owned and uses:

- a connected Cloud Build v2 repository in `us-central1` and the exact source
  SHA;
- `E2_STANDARD_2` and `CLOUD_LOGGING_ONLY`;
- 1,800 seconds for normal profiles and 3,600 seconds for SanPlat profiles;
- no private pool, extra disk, automatic machine escalation or full-build retry;
- no source tarball and no repository-owned `cloudbuild.yaml`;
- a digest-pinned, prebuilt executor image; tooling is not rebuilt in a release;
- one service image push at most for deploys, and no image build for rollbacks.

PR and `main` quality runs execute the complete server-owned profile once.
Deploys consume the exact existing evidence and do not repeat tests, coverage,
scanners, PostgreSQL or semantic-release. A failed or uncertain submit is first
reconciled by fingerprint. After a 120-second absence check the release plane
permits only one retry; another uncertain response remains `UNKNOWN` for an
operator instead of spending quota blindly.

## One-time bootstrap

1. Create the regional Cloud Build v2 GitHub connection in `us-central1` and
   have the organization owner approve the Cloud Build GitHub App.
2. Link the six catalog repositories. Put their full connected-repository
   resource names in `ENG_PLATFORM_CLOUD_BUILD_REPOSITORIES_JSON`, keyed by
   catalog service name.
3. Build `docker/release-executor` once in its controlled image pipeline and
   set `ENG_PLATFORM_RELEASE_EXECUTOR_IMAGE` to an immutable `@sha256:` digest.
   The legacy `ENG_PLATFORM_CLOUD_BUILD_EXECUTOR_IMAGE` is accepted during
   migration only; if both are present they must be identical.
4. Build the three release-orchestration images once and pin all three by
   digest:

   - `ENG_PLATFORM_QUALITY_NODE_IMAGE` from
     `docker/quality-executor/Dockerfile.node`;
   - `ENG_PLATFORM_QUALITY_PYTHON_IMAGE` from
     `docker/quality-executor/Dockerfile.python`;
   - `ENG_PLATFORM_RELEASE_PLANNER_IMAGE` from
     `docker/release-planner/Dockerfile`.

   SanPlat API also requires a reviewed PostgreSQL image pinned in
   `ENG_PLATFORM_RELEASE_POSTGRES_IMAGE`; it is used only as an ephemeral test
   database and never contains production data.

5. Create dedicated runtime identities as described below. Never use the
   default Compute Engine service account and never grant Editor.
6. Create the private evidence buckets and the Firestore collections
   `deployment_executions`, `release_executions`, `executor_circuits` and
   `github_webhook_deliveries`.
7. Keep `ENG_PLATFORM_CLOUD_BUILD_ENABLED=false` and
   `ENG_PLATFORM_RELEASE_ORCHESTRATOR_ENABLED=false` until the connection,
   IAM, webhook, callback identities and reconciliation scheduler are ready.

## Minimum IAM boundaries

Use a separate service account for each execution plane. Within a plane, the
current executors obtain their callback token directly from the attached
service account through the metadata server. Consequently these pairs must
resolve to the same email:

```text
ENG_PLATFORM_RELEASE_QUALITY_SERVICE_ACCOUNT=projects/PROJECT/serviceAccounts/quality@PROJECT.iam.gserviceaccount.com
ENG_PLATFORM_RELEASE_CALLBACK_SERVICE_ACCOUNT=quality@PROJECT.iam.gserviceaccount.com
ENG_PLATFORM_CLOUD_BUILD_SERVICE_ACCOUNT=projects/PROJECT/serviceAccounts/deploy@PROJECT.iam.gserviceaccount.com
ENG_PLATFORM_CLOUD_BUILD_CALLBACK_SERVICE_ACCOUNT=deploy@PROJECT.iam.gserviceaccount.com
```

Do not configure a distinct callback identity until the executor explicitly
supports and the API verifies service-account impersonation.

Use a third, distinct `ENG_PLATFORM_RELEASE_RECONCILER_SERVICE_ACCOUNT` for
Cloud Scheduler. It receives Cloud Run Invoker only and cannot equal the
quality build identity. This prevents repository code from using a build token
to invoke the account-wide reconciler.

| Identity | Required capability | Must not have |
|---|---|---|
| API runtime | Read/write only the four private Firestore collections and the dedicated quality-evidence objects; create/get/list/cancel Cloud Builds; use the connected repositories; act as only the two build service accounts; receive only its named Secret Manager values through Cloud Run | Cloud Run deploy authority through unrelated projects; broad project Editor |
| Release quality build/callback (`ENG_PLATFORM_RELEASE_QUALITY_SERVICE_ACCOUNT`) | Pull the three executor images, write Cloud Logging, run only the declared quality profile, obtain its OIDC token and invoke the execution-scoped API callbacks | Artifact Registry write, Cloud Run traffic, GitHub contents write, production secrets, release reconciliation |
| Release reconciler (`ENG_PLATFORM_RELEASE_RECONCILER_SERVICE_ACCOUNT`) | Invoke only the API service from Cloud Scheduler using OIDC | Build execution, Artifact Registry, GCS, Secret Manager, GitHub or production deploy permissions |
| Deployment build/callback (`ENG_PLATFORM_CLOUD_BUILD_SERVICE_ACCOUNT`) | Pull/push only the target Artifact Registry repositories, deploy/update traffic only for catalog resources, act as only the required runtime service accounts, write the deployment summary bucket and logs, obtain its OIDC token and invoke the API, plus profile-specific auxiliary resources | Project Editor, owner, IAM administration, unrelated services |

Grant `iam.serviceAccounts.actAs` only on the exact build/runtime service
accounts that the caller attaches. Prefer custom roles constrained to these
capabilities; if predefined roles are used, bind them at the Artifact Registry
repository, bucket, Cloud Run service and service-account resource rather than
at project scope. No JSON service-account key is created: GitHub uses WIF/OIDC,
Cloud Build uses its attached identity, and the scheduler uses OIDC.

The GitHub App needs repository metadata and pull-request read, Actions
read/write, Checks write, Contents write and Deployments write. Cloud Build
never receives that write credential. It can request a one-time, short-lived
installation token restricted to the exact repository and `contents:read`;
the API remains the only release publisher.

Repository code in a Cloud Build step can reach the attached build identity
through the metadata server. The release-quality account must therefore stay
limited to Artifact Registry read, Cloud Logging write and API invocation. Do
not grant it Secret Manager, bucket write, Cloud Build create, Artifact
Registry write or production deploy roles. The trusted preparation step claims
the single-issuance source and event tokens before repository code starts and
stores the callback token only in an unmounted root-only control volume.

## Storage retention

Use dedicated buckets so lifecycle deletion cannot affect application data.
Retain only compact JSON evidence and summaries for 30 days; Firestore keeps
the execution identity, hashes, timing, publication and audit state.

For a dedicated deployment-summary bucket, a bucket-wide lifecycle file is:

```json
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"age": 30}
    }
  ]
}
```

Save that document as a reviewed lifecycle file and apply it with:

```bash
gcloud storage buckets update gs://DEPLOYMENT_EVIDENCE_BUCKET \
  --lifecycle-file=retention-30-days.json
```

Apply the same 30-day policy to a dedicated quality-evidence bucket. If either
bucket is shared, use `matchesPrefix` for `deployment-summaries/` or the
configured `ENG_PLATFORM_QUALITY_PREFIX`; never apply a bucket-wide rule to
unrelated data. Verify the lifecycle configuration after applying it with
`gcloud storage buckets describe`.

## Cost accounting and alerts

For release/quality builds the reconciler records provider queue time, execution
duration, estimated minutes and an estimated compute cost on the private
`release_executions` record. Configure the rate rather than treating the code
default as an invoice:

```text
ENG_PLATFORM_CLOUD_BUILD_MINUTE_PRICE_USD=0.006
ENG_PLATFORM_CLOUD_BUILD_USAGE_ALERT_MINUTES=2000,2250,2500
```

Update the minute price when the applicable Google Cloud price changes. The
three thresholds are emitted once per billing owner, UTC month and threshold.
They are warnings, not a hard spending cap, and the estimate excludes storage,
networking, logging and other invoice adjustments. Cloud Build remains
available beyond the free tier; operators must not respond by increasing the
machine class or adding retries. Route the structured warning containing
`cloud_build_usage_threshold` to the operational alert channel; otherwise the
threshold is only a Cloud Logging record.

## Required deployment settings

```text
ENG_PLATFORM_GITHUB_BILLING_OWNER
ENG_PLATFORM_GITHUB_INCLUDED_PRIVATE_MINUTES=2000
ENG_PLATFORM_CLOUD_BUILD_ENABLED
ENG_PLATFORM_DEPLOY_EXECUTOR_MODE=auto
ENG_PLATFORM_CLOUD_BUILD_PROJECT_ID
ENG_PLATFORM_CLOUD_BUILD_REGION=us-central1
ENG_PLATFORM_CLOUD_BUILD_SERVICE_ACCOUNT
ENG_PLATFORM_RELEASE_EXECUTOR_IMAGE=<image>@sha256:<digest>
ENG_PLATFORM_DEPLOYMENT_EXECUTION_FIRESTORE_COLLECTION=deployment_executions
ENG_PLATFORM_CLOUD_BUILD_EVIDENCE_BUCKET
ENG_PLATFORM_CLOUD_BUILD_REPOSITORIES_JSON
ENG_PLATFORM_CLOUD_BUILD_CALLBACK_SERVICE_ACCOUNT
ENG_PLATFORM_CLOUD_BUILD_ENABLED_SERVICES
```

`deployment_executions` and `release_executions` are private operational
records. They store the provider and build/run identifiers, but public
deployment, release, REST and MCP responses intentionally do not expose which
executor ran.

## Validation rule

Automated tests must use mocks, fake Firestore/storage and generated request
assertions. They must never call Cloud Build, dispatch a GitHub workflow, create
a tag, publish a release, deploy or move traffic. The only authorized real
Cloud Build validation during rollout is the single `eng-platform-web` canary,
its manual deploy and its rollback described in the release-orchestrator
runbook. Do not run synthetic builds for the other five services.
