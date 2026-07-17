# Runbook - Production Incident Response

## First Principles

- Classify before acting.
- Do not deploy manually during incident triage.
- Do not move traffic unless the incident is confirmed as traffic regression.
- Capture snapshots before rollback.
- Do not print secrets or customer data.

## Classification

- Traffic regression.
- Wrong image/template.
- Browser/cache issue.
- Frontend/API mismatch.
- Code regression.
- Data source/configuration regression.

## Safe Read-Only Commands

- `gcloud run services describe`
- `gcloud run revisions describe`
- `gcloud run revisions list`
- `gcloud logging read`
- `curl` smoke checks
- `gh run view`

