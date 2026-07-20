# Deploy Any Integrated Service

Use this runbook when a service is already registered in the Engineering
Platform catalog and is marked deployment-ready.

## Before You Start

- You are signed in to Engineering Platform with GitHub.
- Your GitHub login is allowlisted by `ENG_PLATFORM_ALLOWED_GITHUB_LOGINS`.
- The service has a semantic-release tag such as `v1.2.3`.
- The service card in `/deployments` does not show readiness blockers.

You do **not** need GCP Console, a service account JSON key, local GCP
credentials, `gcloud run deploy`, `gh workflow run`, or direct GitHub API calls.

## Deploy Flow

1. Open Engineering Platform Web.
2. Go to `/deployments`.
3. Search for the Cloud Run service name.
4. Open the service deployments page.
5. Select an eligible release tag.
6. Confirm service, tag, commit SHA, current revision, and environment.
7. Watch the deployment stages:
   - `VERIFYING_RELEASE`
   - `BUILDING`
   - `DEPLOYING_CANDIDATE`
   - `VALIDATING_CANDIDATE`
   - `PROMOTING`
   - `VALIDATING_PRODUCTION`
   - `SUCCEEDED`
8. Open the workflow logs if a stage fails.
9. Use the rollback action from deployment history only when returning to a
   previously successful production revision.

## If a Service Is Blocked

If `/deployments` shows readiness blockers, the service is cataloged but not
fully adopted. Open `/factory`, generate the adoption artifacts, and use the
agent handoff prompt to create PR-ready changes in the service repo and the
platform catalog.

Do not bypass readiness with manual Cloud Run commands. Manual `gcloud` traffic
operations are reserved for authorized incident response runbooks only.
