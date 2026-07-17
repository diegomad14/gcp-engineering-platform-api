# Runbook - Rollback PROD

## Goal

Move traffic back to known-good Cloud Run revisions without deploying new images.

## Preconditions

- Target revisions exist.
- Target revisions are Ready.
- Operator provides explicit confirmation text.
- Operator chooses API rollback, Web rollback, or both.
- Current service state has been snapshotted.

## Command Pattern

```bash
gcloud run services update-traffic SERVICE_NAME \
  --project=PROJECT_ID \
  --region=REGION \
  --to-revisions=REVISION=100
```

## Smoke

- API health.
- Web HEAD.
- Contract/OpenAPI checks when available.
- Browser/CORS check when a web client exists.

## API and Web

The reusable rollback template supports independent API and Web rollback through `rollback_api` and `rollback_web`. Roll back only the service that needs traffic movement. When compatibility requires both services, provide both target revisions and validate both after traffic moves.

## Safety

Rollback must not:

- Build or push images.
- Use `gcloud run deploy`.
- Delete revisions or revision tags.
- Modify Secret Manager, IAM, or GitHub secrets.
