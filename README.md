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
| GET | /api/catalog/apps | List all applications |
| GET | /api/catalog/apps/{id} | Get application by ID |
| GET | /api/releases/summary | Recent release activity |
| GET | /api/quality/summary | SonarQube quality status |
| GET | /api/metrics/cloud-run/summary | Cloud Run metrics |
| GET | /api/costs/summary | Cost summary |
| GET | /api/costs/by-service | Costs grouped by service |
| GET | /api/costs/by-app | Costs grouped by app label |
| GET | /api/service-factory/templates | Available service templates |
| POST | /api/service-factory/plan | Generate onboarding plan |
