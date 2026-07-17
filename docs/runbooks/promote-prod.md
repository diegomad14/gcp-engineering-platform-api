# Runbook - Promote to PROD

## Goal

Move 100% production traffic to previously validated candidate revisions.

## Preconditions

- Candidate release workflow passed.
- API and Web candidate revisions are known.
- Candidate revisions are Ready.
- Operator provides explicit confirmation text.
- Current active API and Web revisions match the operator-provided expected current revisions.
- Rollback revisions are recorded before traffic moves.

## Promotion

Use `gcloud run services update-traffic` only through the approved workflow or an incident-approved command.

The reusable template supports API and Web independently through `promote_api` and `promote_web`. Defaults must be safe: no service is promoted unless the operator explicitly opts in and provides the candidate revision.

## Post-Promotion Diagnostics

- Active traffic revisions match the requested revisions.
- API `/health`, Web HEAD, OpenAPI path count, and CORS preflight run after traffic moves.
- These diagnostics emit workflow warnings instead of blocking promotion. Treat failures as follow-up operational work or incident signals.

## Emergency Promotion

Use `promote-emergency.yml` only when normal promotion is blocked by stale candidate metadata or non-critical validation failures and the target revisions are already known-good.

- Confirmation must be exactly `EMERGENCY_PROMOTE`.
- The workflow verifies target revisions are Ready.
- The workflow only runs `gcloud run services update-traffic`; it does not run CORS, OpenAPI, digest, or candidate URL gates.
- The workflow snapshots current traffic and prints rollback commands.
- Record the incident reason in the `reason` input.

## Rollback

Print the previous active revisions before moving traffic. Keep these commands in the workflow logs.

## Failure Handling

If API promotion succeeds but Web promotion fails, report the partial state explicitly and use the printed rollback commands. Do not run a manual deploy to repair a failed promotion.
