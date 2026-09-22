# Cloud Build Economy fallback

Cloud Build is an internal fallback for a deployment or rollback already
authorized by Engineering Platform. Operators do not select it from the UI.
Quality, scans, tests, PostgreSQL and semantic-release stay in the release
workflow that created the semantic tag and its exact `oss-v2` evidence.

## One-time bootstrap

1. Create a regional (`us-central1`) Cloud Build v2 GitHub connection and have
   the organization owner approve the Cloud Build GitHub App.
2. Link all six repositories and put the returned resource names in
   `ENG_PLATFORM_CLOUD_BUILD_REPOSITORIES_JSON`. The value is a map keyed by
   catalog service name.
3. Build `docker/release-executor` once, scan it in its normal build pipeline,
   and set `ENG_PLATFORM_CLOUD_BUILD_EXECUTOR_IMAGE` to its immutable
   `@sha256:` digest. Never use a tag in this setting.
4. Create a dedicated executor service account. Grant only Artifact Registry
   writer/read, Cloud Run deploy/traffic permissions, the profile-specific
   auxiliary-resource permissions, evidence-bucket write, and log write. Do
   not grant Editor and do not use the default Compute Engine account.
5. Set `ENG_PLATFORM_CLOUD_BUILD_ENABLED=false` while configuring the
   connection and IAM. Run deploy and rollback canaries in this order:
   Platform API, Platform web/Communications, Bot, SanPlat web, SanPlat API.
   Add only the current canary to
   `ENG_PLATFORM_CLOUD_BUILD_ENABLED_SERVICES`; the default empty allowlist
   prevents accidental builds during bootstrap. After every profile passes
   deploy and rollback, enable all six and retain
   `ENG_PLATFORM_DEPLOY_EXECUTOR_MODE=auto`.

## Fixed economic contract

Every generated build uses `E2_STANDARD_2`, `CLOUD_LOGGING_ONLY`, no private
pool, no extra disk, no retry loop and one push at most. Simple profiles time
out at 1,800 seconds and SanPlat profiles at 3,600 seconds. Store only the
compact deployment summary in the evidence bucket; apply a 30-day lifecycle
rule to that bucket prefix.

Set budget alerts for estimated usage at 2,000, 2,250 and 2,500 Linux-minute
equivalents. Cloud Build remains available after GitHub's included quota; do
not change the machine class as a response.

## Required settings

`ENG_PLATFORM_GITHUB_BILLING_OWNER`,
`ENG_PLATFORM_GITHUB_INCLUDED_PRIVATE_MINUTES=2000`,
`ENG_PLATFORM_CLOUD_BUILD_ENABLED`,
`ENG_PLATFORM_DEPLOY_EXECUTOR_MODE=auto`,
`ENG_PLATFORM_CLOUD_BUILD_PROJECT_ID`,
`ENG_PLATFORM_CLOUD_BUILD_REGION=us-central1`,
`ENG_PLATFORM_CLOUD_BUILD_SERVICE_ACCOUNT`,
`ENG_PLATFORM_CLOUD_BUILD_EXECUTOR_IMAGE`,
`ENG_PLATFORM_CLOUD_BUILD_EVIDENCE_BUCKET`,
`ENG_PLATFORM_CLOUD_BUILD_REPOSITORIES_JSON`, and
`ENG_PLATFORM_CLOUD_BUILD_CALLBACK_SERVICE_ACCOUNT`, plus the rollout
allowlist `ENG_PLATFORM_CLOUD_BUILD_ENABLED_SERVICES`.

The `deployment_executions` Firestore collection is private operational state.
It stores provider, fingerprint, build ID, digest, revisions and event sequence;
the public deployment response intentionally has no provider field.
