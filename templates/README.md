# Templates

Templates in this directory are inert. They are not active GitHub Actions workflows until copied into an application repository under `.github/workflows/`.

Before adoption:

1. Replace placeholders.
2. Review service-specific secrets and variables.
3. Add behavior-level gates.
4. Run `actionlint`.
5. Open a PR in the application repository.

## Release Templates

- `release-candidate.yml` builds and deploys no-traffic candidate revisions.
- `promote-prod.yml` moves production traffic after strict revision checks. Post-promotion CORS, Web HEAD, and OpenAPI diagnostics are warnings.
- `promote-emergency.yml` is incident-only. It moves traffic to known Ready revisions with confirmation `EMERGENCY_PROMOTE` and skips non-critical validation.
- `rollback-prod.yml` moves traffic back to known-good revisions.

Set `PLATFORM_API_URL` as a repository variable in adopting service repositories to register release events through `POST /api/releases`. If it is absent, release registration is skipped without blocking deployment.

## Quality Gate

New services should use `github-actions/pr-check.yml`, which calls the open
source reusable quality gate. Configure `ENG_PLATFORM_API_URL` and the
`QUALITY_API_TOKEN` secret. `sonar-project.properties.tpl` is deprecated and is
kept only for legacy repositories during migration.
