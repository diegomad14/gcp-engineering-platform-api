# Runbook - Production Incident Response

## First Principles

- Classify before acting.
- Do not deploy manually during incident triage.
- Do not move traffic unless the incident is confirmed as traffic regression.
- Capture snapshots before rollback.
- Do not print secrets or customer data.

## GitHub Actions blocked

For an explicit GitHub billing, spending-limit or included-minute rejection,
preserve the original SHA, semantic tag, GitHub Deployment ID and evidence.
Engineering Platform will use its internal Cloud Build fallback. Do not create
a parallel tag or use `gh workflow run` for a normal deployment.

Manual Cloud Run deployment is break-glass only: it requires an incident
approver, a traffic snapshot, `Ready=True`, immutable image evidence, smoke
checks, rollback details and later `Manual/untracked` reconciliation.

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
