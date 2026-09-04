# Templates

Templates in this directory are inert. They are not active GitHub Actions workflows until copied into an application repository under `.github/workflows/`.

Before adoption:

1. Replace placeholders.
2. Review service-specific secrets and variables.
3. Add behavior-level gates.
4. Run `actionlint`.
5. Open a PR in the application repository.

## GitHub Actions runner contingency

Generated release/deploy/rollback workflows resolve `runs-on` from the
optional `runner_label` input and repository variable `CGM_ACTIONS_RUNNER`.
The normal setting is `ubuntu-latest`. The only fallback is
`cgm-release-local`, executed inside the disposable Linux x64 VM launched by
the local emergency runner tool. Pull-request and quality workflows remain on
hosted runners.

The local runner is repository-scoped, has Docker plus the workflow toolchain,
uses WIF/OIDC for GCP and is destroyed after the incident. Never add a JSON
service-account key, PAT or host credential to the VM or repository. Restore
`ubuntu-latest` and record the canary after the incident. See
`scripts/ops/local-release-runner/README.md` and the canonical
`release_process` runbook.

## Release Templates

- `platform-deploy.yml` is the canonical GitHub-native workflow. It verifies an
  immutable tag, builds once, deploys a zero-traffic candidate, validates,
  promotes, validates production and rolls back automatically when required.

The platform dispatches this workflow through GitHub Deployments. The legacy
manual candidate/promote/rollback templates were removed because their
`workflow_dispatch` interfaces exceeded GitHub's supported input limit.

## Quality Gate

New services should use `github-actions/pr-check.yml`, which calls the open
source reusable quality gate. Configure `ENG_PLATFORM_API_URL` and the
`QUALITY_API_TOKEN` secret. The Sonar template has been retired and is
kept only for legacy repositories during migration.
