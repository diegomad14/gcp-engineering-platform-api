# Templates

Templates in this directory are inert. They are not active GitHub Actions workflows until copied into an application repository under `.github/workflows/`.

Before adoption:

1. Replace placeholders.
2. Review service-specific secrets and variables.
3. Add behavior-level gates.
4. Run `actionlint`.
5. Open a PR in the application repository.

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
`QUALITY_API_TOKEN` secret. `sonar-project.properties.tpl` is deprecated and is
kept only for legacy repositories during migration.
