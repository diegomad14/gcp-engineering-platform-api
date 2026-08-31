# Runbook - Production Incident Response

## First Principles

- Classify before acting.
- Do not deploy manually during incident triage.
- Do not move traffic unless the incident is confirmed as traffic regression.
- Capture snapshots before rollback.
- Do not print secrets or customer data.

## GitHub Actions blocked

If a workflow does not start because of payment failure, spending limit,
included quota, an unavailable runner, or a GitHub incident, use the canonical
Obsidian runbook `Runbook - GitHub Actions bloqueado`, linked from
`release_process`. Preserve the original commit SHA, semantic tag, GitHub
Deployment ID and evidence. Do not create a parallel tag or use
`gh workflow run` for a normal deployment.

The documented fallback is the repository variable
`CGM_ACTIONS_RUNNER=cgm-release-local`, only after confirming the disposable
Linux x64 VM runner is online and labeled correctly. Restore
`CGM_ACTIONS_RUNNER=ubuntu-latest` after the canary and record the incident.

For an alert/health check, run
`bash scripts/ops/check_github_runner.sh OWNER/REPOSITORY cgm-release-local`. It
returns non-zero when there is no online idle runner with the expected label.

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
