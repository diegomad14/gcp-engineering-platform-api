# Engineering Platform API

FastAPI control plane for independent Cloud Run services.

## Local development

```bash
cd apps/platform-api
pip install -e ".[dev]"
uvicorn eng_platform_api.main:app --reload --port 8000
python3 -m pytest -q
```

## Main endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/health` | API health |
| GET | `/api/health/services` | Cloud Run readiness per service |
| GET | `/api/catalog/services` | Flat service catalog |
| GET | `/api/catalog/services/{service_name}` | Service metadata and live state |
| POST | `/api/releases` | Register and split a release event by service |
| GET | `/api/releases` | Service release rows |
| GET | `/api/releases/summary` | Stored and GitHub-discovered release rows |
| GET | `/api/releases/{service_name}/latest` | Latest release for one service |
| GET | `/api/services/{service_name}/tags` | Paginated eligible GitHub release tags |
| POST | `/api/services/{service_name}/deployments` | Idempotently dispatch a service deployment |
| GET | `/api/services/{service_name}/deployments` | Deployment history reconstructed from GitHub |
| GET | `/api/deployments/{deployment_id}` | Jobs, stages, URLs and evidence for one deployment |
| GET | `/api/costs/summary` | Billing summary |
| GET | `/api/costs/by-service` | Billing rows by service |
| POST | `/api/quality/reports` | Register normalized CI evidence (Bearer token required) |
| GET | `/api/quality/summary` | Latest gate status per independent service |
| GET | `/api/quality/services/{service_name}/commits/{sha}` | Exact deploy evidence for a commit |
| GET | `/api/quality/services/{service_name}/reports` | Recent quality history for a service |
| POST | `/api/service-factory/plan` | Generate service onboarding artifacts |

## Quality report storage

- `ENG_PLATFORM_QUALITY_INGEST_TOKEN`: required Bearer token for report writes.
- `ENG_PLATFORM_QUALITY_BUCKET`: private GCS bucket used in production.
- `ENG_PLATFORM_QUALITY_PREFIX`: object prefix, default `quality`.
- `ENG_PLATFORM_QUALITY_STORE_PATH`: local development root, default `data`.
- `ENG_PLATFORM_QUALITY_STALE_AFTER_HOURS`: evidence validity, default `168`.

The Cloud Run runtime service account needs object create/read permissions on the
configured bucket. Reports are idempotent by `service_name + commit_sha`.

## GitHub deployment configuration

- `ENG_PLATFORM_GITHUB_ENABLED=true`
- `ENG_PLATFORM_GITHUB_TOKEN` for development, or the GitHub App variables
  `ENG_PLATFORM_GITHUB_APP_ID`, `ENG_PLATFORM_GITHUB_INSTALLATION_ID` and
  `ENG_PLATFORM_GITHUB_PRIVATE_KEY` in production.
- `ENG_PLATFORM_GITHUB_DEPLOYMENT_WORKFLOW`, default `platform-deploy.yml`.
- `ENG_PLATFORM_DEPLOYMENT_FIRESTORE_COLLECTION` enables durable minimal
  metadata; local development falls back to `data/deployments.json`.

GitHub remains authoritative for tags, workflow state, jobs and logs.

## Release request

```json
{
  "repository": "diegomad14/parametrizacion-correos-cgm",
  "version": "v0.9.56",
  "status": "promoted",
  "services": [
    {
      "service_name": "cgm-sanplat-api",
      "revision": "cgm-sanplat-api-00006-jiw",
      "action": "promoted"
    }
  ],
  "github_run_url": "https://github.com/example/repo/actions/runs/123"
}
```

`v0.4.0` removes application endpoints and the public `app_id`, `app_name`,
`api_revision`, `web_revision`, and cost `app` fields.
