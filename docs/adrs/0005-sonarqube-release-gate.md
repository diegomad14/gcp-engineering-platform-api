> Historical: superseded by [ADR 0008](../adrs/0008-oss-only-release-quality.md).
> New releases use the mandatory OSS gate; the instructions below are retained as history.

# ADR 0005 - SonarQube Cloud Release Gate

## Status

Superseded by ADR 0007. The reusable Sonar workflow remains temporarily for
legacy callers but is no longer generated for new services.

## Context

Code quality analysis is needed as part of the release process. SonarQube Cloud provides static analysis, coverage tracking, and quality gates. The platform must provide a reusable integration that app repos can consume without duplicating configuration.

## Decision

1. **Reusable workflow** — Provide `reusable-sonarqube.yml` using `workflow_call` with `SonarSource/sonarqube-scan-action@v5`.
2. **Progressive adoption** — Start with non-blocking mode (`continue-on-error: true` or `sonar.qualitygate.wait=false`). Transition to blocking after baseline quality gate passes.
3. **Conditional execution** — If `sonar-enabled=false` or `SONAR_TOKEN` is missing:
   - Non-blocking: skip clearly with a log message.
   - Blocking: fail clearly with an actionable error.
4. **Configuration surface** — Inputs: `sonar-enabled`, `sonar-project-key`, `sonar-organization`, `blocking-quality-gate`, `working-directory`. Secrets: `SONAR_TOKEN`, `SONAR_HOST_URL`.

## Required Configuration

```yaml
# sonar-project.properties (in app repo)
sonar.organization=<org>
sonar.projectKey=<org_repo>
sonar.sources=src
sonar.sourceEncoding=UTF-8
```

## Integration into Release Process

```
PR opened
  → tests pass
  → build passes
  → SonarQube scan
  → quality gate (non-blocking initially)
  → PR merge

Release candidate
  → only if PR checks passed
  → SonarQube status reported in release summary
```

## Consequences

- App repos get SonarQube integration without maintaining their own workflow.
- Quality gate can be enforced at the PR level once baseline is established.
- Token management remains with the app repo (SONAR_TOKEN secret).
- Platform does not store or proxy SonarQube credentials.
