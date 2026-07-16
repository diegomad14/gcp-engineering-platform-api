# Engineering Platform API

FastAPI application for the Engineering Platform Control Plane.

## Running Locally

```bash
cd apps/platform-api
pip install -e ".[dev]"
uvicorn eng_platform_api.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs

## Running Tests

```bash
cd apps/platform-api
python -m pytest -q
```

## Configuration

All integrations default to mock mode. Set environment variables to enable real GCP/GitHub/SonarQube integrations:

```
ENG_PLATFORM_MOCK_MODE=false
ENG_PLATFORM_BILLING_ENABLED=true
ENG_PLATFORM_BQ_PROJECT_ID=my-project
ENG_PLATFORM_BQ_DATASET=billing_export
ENG_PLATFORM_BQ_TABLE=gcp_billing_export_resource_v1_XXXXXX
ENG_PLATFORM_MONITORING_ENABLED=true
ENG_PLATFORM_GCP_PROJECT_ID=my-project
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | /health | Health check |
| GET | /api/health/services | Aggregate catalog service health |
| GET | /api/catalog/apps | List all applications |
| GET | /api/catalog/apps/{id} | Get application by ID |
| POST | /api/releases | Register a release event |
| GET | /api/releases | List stored release events |
| GET | /api/releases/summary | Recent release activity |
| GET | /api/releases/{app_id}/latest | Latest release for an application |
| GET | /api/quality/summary | SonarQube quality status |
| GET | /api/metrics/cloud-run/summary | Cloud Run metrics |
| GET | /api/costs/summary | Cost summary |
| GET | /api/costs/by-service | Costs grouped by service |
| GET | /api/costs/by-app | Costs grouped by app label |
| GET | /api/service-factory/templates | Available service templates |
| POST | /api/service-factory/plan | Generate onboarding plan |

## Release History Contract

Release responses are multiservice. Each release contains a canonical
`services[]` list instead of fixed API/Web revision fields:

```json
{
  "app_id": "cgm-integration-platform",
  "app_name": "CGM Integration Platform",
  "version": "v0.9.56",
  "status": "promoted",
  "services": [
    {
      "service_name": "cgm-sanplat-api",
      "revision": "cgm-sanplat-api-00006-jiw",
      "action": "promoted"
    },
    {
      "service_name": "cgm-sanplat-web",
      "revision": "",
      "action": "not_included"
    }
  ],
  "github_run_url": "https://github.com/example/repo/actions/runs/123",
  "created_at": "2026-07-16T18:56:35Z"
}
```

Supported service actions are `promoted`, `deployed`, `rolled_back`,
`unchanged`, `not_included`, and `missing`.

- Services explicitly sent by the webhook retain their action.
- Catalog services omitted from a non-empty webhook payload are returned as
  `not_included`.
- Empty, incomplete, GitHub-discovered, or unrecoverable legacy service data
  is returned as `missing`.
- Historical fixed `api_revision` and `web_revision` records remain readable,
  but those fields are no longer part of API responses.

> **Breaking change (SCRUM-38):** API consumers must read `services[]`.
> `api_revision` and `web_revision` were removed from `ReleaseItem`.
