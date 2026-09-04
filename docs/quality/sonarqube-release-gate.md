> Historical: superseded by [ADR 0008](../adrs/0008-oss-only-release-quality.md).
> New releases use the mandatory OSS gate; the instructions below are retained as history.

# SonarQube Cloud Release Gate

> Deprecated. New and migrated services use
> [Open Source Quality Gate](open-source-quality-gate.md). This document remains
> only for repositories that have not completed migration.

## Overview

SonarQube Cloud provides static code analysis, security scanning, and quality gates. The platform integrates SonarQube as a reusable workflow that app repos call during PR checks.

## Reusable Workflow

**File:** `.github/workflows/reusable-sonarqube.yml`

### Inputs

| Input | Type | Default | Description |
|-------|------|---------|-------------|
| `sonar-enabled` | boolean | `false` | Enable SonarQube scan |
| `sonar-project-key` | string | `""` | SonarQube project key |
| `sonar-organization` | string | `""` | SonarQube organization |
| `blocking-quality-gate` | boolean | `false` | Wait for quality gate result |
| `working-directory` | string | `"."` | Project root for scan |

### Secrets

| Secret | Required | Description |
|--------|----------|-------------|
| `SONAR_TOKEN` | Conditional | SonarQube Cloud token |
| `SONAR_HOST_URL` | No | SonarQube server URL (default: sonarcloud.io) |

## Behavior Matrix

| `sonar-enabled` | `SONAR_TOKEN` | `blocking-quality-gate` | Behavior |
|-----------------|---------------|------------------------|----------|
| `false` | any | any | Skip, job not executed |
| `true` | Missing | `false` | Skip with clear log message |
| `true` | Missing | `true` | **Fail** with actionable error |
| `true` | Present | `false` | Scan, report, don't block |
| `true` | Present | `true` | Scan, wait for quality gate, fail on gate failure |

## Integration Points

### PR Check
```
PR check workflow:
  1. backend tests
  2. frontend build
  3. SonarQube scan (optional, non-blocking initially)
  4. OpenAPI validation
```

### Release Candidate
```
Release candidate summary includes:
  - SonarQube project URL
  - Quality gate status (if available)
  - Coverage percentage
```

## Progressive Adoption Path

1. **Phase 1:** Enable SonarQube with `blocking-quality-gate=false`. Observe baseline.
2. **Phase 2:** Fix critical issues identified by SonarQube.
3. **Phase 3:** Enable `blocking-quality-gate=true` after quality gate passes consistently.
4. **Phase 4:** Add SonarQube status check as required in GitHub branch protection.

## sonar-project.properties Template

```properties
sonar.organization=<ORG_KEY>
sonar.projectKey=<ORG>_<REPO>
sonar.projectName=<Human Readable Name>
sonar.sources=src
sonar.sourceEncoding=UTF-8
sonar.python.version=3.11
```

## Required GitHub Secrets (App Repo)

- `SONAR_TOKEN` — Generated from SonarQube Cloud: My Account → Security → Generate Tokens.

## Security

- Token is passed as a GitHub secret, never stored in the platform repo.
- Platform workflow references `${{ secrets.SONAR_TOKEN }}` — no hardcoded values.
- Token is never printed in logs.
