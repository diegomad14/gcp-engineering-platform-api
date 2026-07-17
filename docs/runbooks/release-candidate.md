# Runbook - Release Candidate

## Goal

Build images, deploy no-traffic candidate revisions, assign candidate revision tags, and validate tagged URLs before any production traffic move.

## Required Checks

- SemVer tag format.
- Image pushed to Artifact Registry.
- Candidate revisions created and Ready.
- Candidate tag URLs point to the exact candidate revisions.
- API tagged `/health` returns 200.
- OpenAPI count and critical paths pass when applicable.
- Web tagged HEAD returns 200.
- CORS preflight passes for the intended browser origin.
- Configurable smoke endpoints do not return 5xx.
- Candidate gate report includes API revision, Web revision, tagged URLs, image digests, and `safe_to_promote`.

## Safety

Do not use production service URLs to prove a candidate revision is healthy. Service URLs may still route to the previous production revision.

The release-candidate workflow must deploy with `--no-traffic` and must not contain `--to-revisions`. Production traffic movement belongs in the manual promote workflow.

## Candidate URL Validation

For Cloud Run revision tags, validate the tagged URL from `status.traffic[].url`. The gate must also verify that the tagged URL belongs to the exact revision created by the candidate deploy.

Minimum candidate validation:

```text
API tagged URL /health -> HTTP 200
API tagged URL /openapi.json -> expected path count
API tagged URL /openapi.json -> critical paths present
Web tagged URL HEAD / -> HTTP 200
CORS OPTIONS API tagged /health with Origin: Web tagged URL -> 2xx
Configured smoke endpoints -> no 5xx
```

## Data Sources

If the candidate depends on private files, use `docs/runbooks/gcs-private-data-source.md`. Do not commit database files, CSV exports, dumps, or PII to the service repository or platform repository.
