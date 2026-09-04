# Platform-only release runbook

## One-time control-plane bootstrap

After merging the control-plane PR, run **Bootstrap Platform Runtime** from the
`main` branch with the new semantic-release tag. It is restricted to
`diegomad14`, deploys a zero-traffic candidate, smokes `/health`, promotes it,
and restores the previous revision if the bootstrap fails.

Once it succeeds, verify one normal release from Engineering Platform and then
remove the six legacy `cgm-github-pool` deployer bindings. The bootstrap
workflow intentionally depends on those bindings, so it becomes inoperable
after cutover. Do not use it for routine service releases.

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
