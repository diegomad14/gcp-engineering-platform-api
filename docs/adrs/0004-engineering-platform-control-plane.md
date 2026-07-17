# ADR 0004 - Engineering Platform Control Plane

## Status

Proposed

## Context

The platform repository provides reusable patterns, templates, runbooks, and governance for GCP Cloud Run services. Currently all workflows are copy-only templates under `templates/github-actions/`. App repositories must manually copy and adapt them. There is no centralized API, UI dashboard, cost visibility, or automated service onboarding.

## Decision

Build an Engineering Platform Control Plane MVP with these components:

1. **Reusable GitHub Actions workflows** using `workflow_call` — stored in `.github/workflows/`, callable from app repos via `uses:`.
2. **Platform API** (FastAPI) — read-only endpoints for catalog, releases, quality, metrics, costs, and service factory. Mock-backed by default.
3. **Platform Web UI** (React + Vite + TypeScript) — dashboard and operational views. No deploy button.
4. **BigQuery Billing Export** — read-only SQL templates and Python query builder for cost attribution.
5. **Cloud Monitoring** — metric definitions and mock-backed summary endpoint for Cloud Run services.
6. **Service Factory** — YAML contract generator, caller workflow templates, onboarding checklist.

## Architecture Principles

- **Source of truth:** GitHub repo + YAML contracts for metadata; BigQuery for billing; Cloud Monitoring for metrics.
- **Execution engine:** GitHub Actions (WIF/OIDC → GCP).
- **Quality service:** SonarQube Cloud.
- **Runtime (optional):** Cloud Run with min-instances=0. Platform API/Web are not required for app releases to function.
- **No database for MVP.** Billing data lives in BigQuery. Operational data lives in Cloud Monitoring. Config lives in YAML/GitHub.
- **Security:** WIF/OIDC only. No GCP_SA_KEY. No service account JSON. No secrets in repo.

## Consequences

- App repos can call platform workflows directly instead of maintaining their own copies.
- Platform API/Web provide operational visibility without requiring GCP console access.
- Service Factory reduces onboarding friction for new services.
- BigQuery cost model enables FinOps practices without additional tooling.
- All platform components are optional — app releases work with workflows alone.
