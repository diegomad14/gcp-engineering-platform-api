# Platform-only release runbook

## Completed control-plane transition

The v0.19.0 compatibility bootstrap completed on 2026-09-04, run 33923346159.
Its temporary workflow has been removed. New releases publish oss-v2 reports and
use the signed Platform Deploy path below. The original bootstrap evidence and
revision remain available for the audit trail.

Engineering Platform is the only normal release entrypoint. An allowlisted
GitHub user signs in to the web UI, selects an immutable release tag, and
starts the candidate-to-production pipeline. The pipeline promotes only after
the candidate smoke succeeds and rolls traffic back automatically if the
post-promotion smoke fails.

The platform API creates the GitHub Deployment and signs a five-minute,
one-time authorization. Service workflows must verify and consume that ticket
before authenticating to GCP. A direct `workflow_dispatch` without a ticket is
expected to fail safely.

Production configuration requires:

- `ENG_PLATFORM_ALLOWED_GITHUB_LOGINS` set outside the code repository;
- a stable `ENG_PLATFORM_SESSION_SECRET`;
- GitHub OAuth and GitHub App credentials from Secret Manager;
- `ENG_PLATFORM_RELEASE_SIGNING_PRIVATE_KEY` from Secret Manager;
- `ENG_PLATFORM_RELEASE_AUTH_FIRESTORE_COLLECTION` configured in Cloud Run;
- `ENG_PLATFORM_API_URL` and `ENG_PLATFORM_FRONTEND_URL` matching the current
  Cloud Run service URLs;
- `GCP_RELEASE_WIF_PROVIDER` configured in every catalog service repository.

The normal runner setting is empty, which selects GitHub-hosted runners. Set
`CGM_ACTIONS_RUNNER=cgm-release-local` only for an approved contingency test
after the runner health check reports it online.

Operators do not use `gcloud`, GCP Console, or direct workflow dispatches for
normal deploys and rollbacks. Platform maintainers handle allowlist changes,
key rotation, repository policy changes, and control-plane releases separately
from the daily service release process.

## Release federation (2026-09-04)

The release WIF provider accepts only the Cloud Deploy Platform GitHub App
(actor ID `306096861`), `workflow_dispatch` on a `refs/tags/v*` ref for deploy or `main` for rollback, the six
catalog repositories, and each repository's exact Platform Deploy or Platform
Rollback workflow path. Direct operator dispatch cannot authenticate through
this provider. The action verifies the trusted signing key supplied explicitly
by the caller; composite actions cannot access GitHub's `vars` context.

The previous `ref_protected` condition prevented normal releases because private
repositories on the current GitHub plan cannot configure tag/branch rulesets.
The application identity and exact workflow path replace that condition. Native
quality, exact-SHA oss-v2 evidence, signed one-time authorization and historical
rollback verification remain mandatory in the release workflows. The provider
condition is recorded in `docs/quality/release-wif-policy.json`.

Keep the `cgm-release-local` allowlist and exact-SHA contingency restrictions;
this federation configuration does not authorize arbitrary local runners.

The API validates current exact-SHA quality evidence before creating or retrying
a deployment. Historical tags cannot invoke old deploy workflows to bypass the
policy. Manual rollback always dispatches the current `main` rollback workflow,
with the historical tag, SHA and promoted revision as inputs. This validates
original evidence and image identity without requalifying historical code.
