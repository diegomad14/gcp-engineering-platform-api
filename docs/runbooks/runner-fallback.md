# GitHub Actions runner fallback

The fallback is a capacity contingency for release and rollback workflows. It
does not bypass Engineering Platform authorization, candidate validation,
production environments, or WIF/OIDC.

## Diagnosis order

1. Inspect the active workflow run and confirm it has jobs. Ignore stale runs
   associated with deleted workflows such as `BuildFailed`.
2. Confirm the repository has an online runner with the exact label
   `cgm-release-local`.
3. Confirm the repository variable `CGM_ACTIONS_RUNNER` is either empty or
   exactly `cgm-release-local`.
4. If the runner is unavailable, leave the variable empty so jobs fail over to
   the GitHub-hosted runner instead of queueing indefinitely.

The runner is repository-scoped and ephemeral. It must use the approved image
and pinned runner artifact described in
`scripts/ops/local-release-runner/README.md`.
