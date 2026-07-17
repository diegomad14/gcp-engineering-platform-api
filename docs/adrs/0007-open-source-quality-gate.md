# ADR 0007 - Open Source Quality Gate

## Status

Accepted

## Context

Running SonarQube for every private service and deploy is not cost-effective.
The platform still needs blocking tests, coverage, code-quality and security
evidence, plus a central per-service view.

## Decision

- Use `reusable-quality-gate.yml` once per service and commit.
- Use Ruff or ESLint, project tests/build, Semgrep Community and Trivy.
- Block coverage below 70% for Python and Node profiles.
- Block failed tests/build/lint/type checks, secrets, Semgrep ERROR findings,
  and Trivy HIGH/CRITICAL findings.
- Publish a normalized report to `POST /api/quality/reports`.
- Store reports in a private GCS bucket and expose the latest state through
  `/api/quality/summary`.
- Require release, promotion and rollback workflows to validate an exact
  `service_name + commit_sha` result before changing Cloud Run.
- Keep `reusable-sonarqube.yml` deprecated for migration compatibility only.

## Consequences

- There is no quality-tool license cost; GitHub Actions runtime and GCS storage
  remain normal infrastructure consumption.
- Services sharing a repository retain independent evidence and thresholds.
- Deployment does not repeat analysis and fails closed when evidence is
  missing, failed or stale.
