# Open Source Quality Gate

The platform replaces SonarQube with a composable, blocking workflow based on
open source tools. It runs once for each service and commit, publishes a JSON
report, and lets deploy workflows reuse that evidence.

## Required repository configuration

Variables:

- `ENG_PLATFORM_API_URL`

Secrets:

- `QUALITY_API_TOKEN`

Example:

```yaml
jobs:
  quality:
    uses: diegomad14/gcp-engineering-platform-api/.github/workflows/reusable-quality-gate.yml@v1
    with:
      service-name: example-api
      profile: python
      working-directory: .
      coverage-threshold: 70
      platform-api-url: ${{ vars.ENG_PLATFORM_API_URL }}
    secrets:
      QUALITY_API_TOKEN: ${{ secrets.QUALITY_API_TOKEN }}
```

## Profiles and blocking policy

| Profile | Required checks |
|---|---|
| `python` | pytest coverage, Ruff lint/format, compile check, Semgrep, Trivy |
| `node` | tests and coverage, build, ESLint, TypeScript, Semgrep, Trivy |
| `static` | build, ESLint, Semgrep and Trivy; coverage is not required |

Python and Node fail below 70% coverage. Semgrep runs blocking ERROR rules.
Trivy blocks HIGH/CRITICAL dependency findings, secrets and misconfigurations.
Commands can be overridden through reusable-workflow inputs when a repository
uses a different test runner or directory layout.

The workflow always uploads its report and attempts to register it before
returning a failed status.

## Deployment evidence

Set these inputs on release workflows:

```yaml
platform-api-url: ${{ vars.ENG_PLATFORM_API_URL }}
quality-gate-enabled: true
quality-commit-sha: ${{ github.sha }}
```

Promotion and rollback callers must explicitly provide the commit SHA belonging
to the candidate or known-good revision. Only `PASSED` evidence is accepted;
missing, failed or stale evidence blocks the workflow.

## Platform runtime

Configure `ENG_PLATFORM_QUALITY_INGEST_TOKEN` and a private
`ENG_PLATFORM_QUALITY_BUCKET`. The API writes:

- `quality/reports/<service>/<sha>.json`
- `quality/latest/<service>.json`

Without a bucket, local development writes the same layout below `data/`.
