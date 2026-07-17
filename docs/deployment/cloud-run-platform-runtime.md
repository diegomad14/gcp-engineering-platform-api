# Cloud Run Platform Runtime Deployment

## Status: Documentation Only

**These commands are for reference. Do NOT execute them in this PR.**

Deployment of platform runtime components requires explicit approval and a separate PR.

## Platform Components

| Component | Service Name | Port | Framework |
|-----------|-------------|------|-----------|
| Platform API | `eng-platform-api` | 8000 | Python FastAPI |
| Platform Web | `eng-platform-web` | 80 | React + Vite + nginx |

## Docker Images

### Build

```bash
# Platform API
cd apps/platform-api
docker build --platform linux/amd64 -t eng-platform-api .

# Platform Web
cd apps/platform-web
docker build --platform linux/amd64 -t eng-platform-web .
```

### Push to Artifact Registry

```bash
# Tag and push API
docker tag eng-platform-api \
  us-central1-docker.pkg.dev/cgm-assistant-prod/cgm-sanplat-repo/eng-platform-api:v0.4.0
docker push us-central1-docker.pkg.dev/cgm-assistant-prod/cgm-sanplat-repo/eng-platform-api:v0.4.0

# Tag and push Web
docker tag eng-platform-web \
  us-central1-docker.pkg.dev/cgm-assistant-prod/cgm-sanplat-repo/eng-platform-web:v0.4.0
docker push us-central1-docker.pkg.dev/cgm-assistant-prod/cgm-sanplat-repo/eng-platform-web:v0.4.0
```

## Deploy Commands (Do NOT Execute)

### Platform API

```bash
gcloud run deploy eng-platform-api \
  --project=cgm-assistant-prod \
  --region=us-central1 \
  --image=us-central1-docker.pkg.dev/cgm-assistant-prod/cgm-sanplat-repo/eng-platform-api:v0.4.0 \
  --min-instances=0 \
  --max-instances=2 \
  --cpu=1 \
  --memory=512Mi \
  --concurrency=80 \
  --port=8000 \
  --allow-unauthenticated \
  --service-account=cgm-sanplat-runtime@cgm-assistant-prod.iam.gserviceaccount.com \
  --set-env-vars=ENG_PLATFORM_MOCK_MODE=true,ENG_PLATFORM_QUALITY_BUCKET=<PRIVATE_BUCKET> \
  --set-secrets=ENG_PLATFORM_QUALITY_INGEST_TOKEN=eng-platform-quality-token:latest
```

### Platform Web

```bash
gcloud run deploy eng-platform-web \
  --project=cgm-assistant-prod \
  --region=us-central1 \
  --image=us-central1-docker.pkg.dev/cgm-assistant-prod/cgm-sanplat-repo/eng-platform-web:v0.4.0 \
  --min-instances=0 \
  --max-instances=2 \
  --cpu=1 \
  --memory=256Mi \
  --concurrency=80 \
  --port=80 \
  --allow-unauthenticated \
  --service-account=cgm-sanplat-runtime@cgm-assistant-prod.iam.gserviceaccount.com
```

## Pre-Deployment Checklist

Before deploying platform runtime to production:

- [ ] Platform API tests pass locally.
- [ ] Platform Web builds successfully.
- [ ] Docker images build and run locally.
- [ ] IAP or OAuth configured for UI access control (required before public exposure).
- [ ] Platform runtime service account has minimum required permissions.
- [ ] Quality bucket is private and the runtime service account can create/read objects.
- [ ] `ENG_PLATFORM_QUALITY_INGEST_TOKEN` is mounted from Secret Manager.
- [ ] Cloud Billing Export enabled (if cost features needed).
- [ ] Security review completed.
- [ ] Deployment approval obtained.

## Post-Deployment

- Verify `/health` returns 200.
- Verify UI loads and shows mock data.
- Verify Cloud Run service is running with min-instances=0.
- Monitor for unexpected costs.

## Rollback

```bash
# If a rollback revision is known:
gcloud run services update-traffic eng-platform-api \
  --project=cgm-assistant-prod \
  --region=us-central1 \
  --to-revisions=<KNOWN_GOOD_REVISION>=100

gcloud run services update-traffic eng-platform-web \
  --project=cgm-assistant-prod \
  --region=us-central1 \
  --to-revisions=<KNOWN_GOOD_REVISION>=100
```

## Security Notes

- **DO NOT expose the platform UI publicly without authentication.**
- Configure Identity-Aware Proxy (IAP) or OAuth2 before allowing external access.
- The platform API surfaces operational data — treat it as internal tooling.
- Default mock mode prevents accidental GCP API calls in development.
