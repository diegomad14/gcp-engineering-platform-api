# Engineering Platform Architecture

## Overview

The GCP Engineering Platform provides reusable release automation, operational visibility, cost management, and service onboarding for owned GCP Cloud Run applications. It does not manage third-party systems — external systems (SanPlat, FND, Perseo, OSF) are validation targets only.

## Architecture Diagram

```
Developer
  |
  v
GitHub App Repo (source of truth for app code)
  |
  +--> uses: platform/.github/workflows/reusable-pr-check.yml@v0.15.0
  +--> uses: platform/.github/workflows/reusable-quality-gate.yml@v0.15.0
  +--> uses: platform/.github/workflows/reusable-cloud-run-release-candidate.yml@v0.15.0
  +--> uses: platform/.github/workflows/reusable-cloud-run-promote.yml@v0.15.0
  +--> uses: platform/.github/workflows/reusable-cloud-run-rollback.yml@v0.15.0
  +--> uses: platform/.github/workflows/service-onboarding-plan.yml@v0.15.0
  |
  v
GitHub Actions (Execution Engine)
  |
  +--> OSS quality reports ─────── release evidence
  +--> GCP Cloud Run ───────────── release targets (deploy, promote, traffic)
  +--> GCP Cloud Monitoring ────── read-only metrics
  +--> GCP BigQuery ────────────── read-only billing data
  |
  v
Optional: eng-platform-api + eng-platform-web on Cloud Run
  - min-instances: 0
  - max-instances: 2
  - Auth: documented for IAP/OAuth (not implemented in MVP)
```

## Components

### Release Plane
- **Reusable workflows** in `.github/workflows/` using `workflow_call`.
- **Quality evidence** produced by Ruff/ESLint, tests, Semgrep and Trivy, then
  persisted by the Platform API per service and commit.
- Called from app repos via `uses: diegomad14/gcp-engineering-platform-api/.github/workflows/<name>.yml@v0.15.0`.
- WIF/OIDC authentication to GCP.
- Candidate → validate → promote → rollback lifecycle.

### Ops/FinOps Plane
- **BigQuery** for billing/cost data (read-only).
- **Cloud Monitoring** for operational metrics (read-only).
- Platform API surfaces both via mock-backed endpoints.

### UX Plane
- **Platform Web UI** — React dashboard with catalog, costs, metrics, quality, releases, service factory.
- No deploy button in MVP. View/details/copy-YAML only.
- Auth documented but not enforced in local dev.

### Service Factory
- YAML contract generator.
- Caller workflow templates.
- Onboarding checklist.
- Manual workflow_dispatch for plan generation.

### Data Model
- **No database for MVP.**
- Billing data: BigQuery `gcp_billing_export_resource_v1_*`.
- Metrics data: Cloud Monitoring API.
- Config/catalog: YAML files in GitHub repos.
- Release history: GitHub Actions run history.
- Future (Phase 2): Firestore for UI cache/audit/preferences.

## Security

- WIF/OIDC for all GCP authentication.
- No GCP_SA_KEY, no service account JSON.
- No secrets in repo.
- Platform runtime (optional): dedicated service account with least privilege.

## External Systems (Validation Targets Only)

Systems like SanPlat, FND, Perseo, OSF appear in the catalog as `validation_targets`. They are:
- Smoke-tested during release validation.
- NOT deployed by platform workflows.
- NOT configured by platform workflows.
- Owned and operated by their respective teams.

## Cloud Run min-instances Policy

All platform runtime components (eng-platform-api, eng-platform-web) use:
- `min-instances: 0` — scale to zero when idle.
- `max-instances: 2` — low ceiling for MVP.
- No always-on VM or server.
