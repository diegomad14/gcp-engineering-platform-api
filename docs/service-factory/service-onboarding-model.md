# Service Factory — Service Onboarding Model

## Purpose

The Service Factory reduces friction for creating and onboarding new GCP Cloud Run services into the platform. It generates YAML contracts, caller workflow files, and an onboarding checklist — without creating any real GCP resources.

## Flow

```
Developer fills in service metadata
  |
  v
Service Factory generates:
  ├── gcp-service-release.yaml      (service contract)
  ├── caller-pr-check.yml           (calls reusable PR check)
  ├── caller-release-candidate.yml  (calls reusable release candidate)
  ├── caller-promote.yml            (calls reusable promote)
  ├── caller-rollback.yml           (calls reusable rollback)
  ├── .quality-gate.yml             (open source quality policy)
  ├── cloud-run-service-labels.yaml (label manifest)
  └── onboarding-checklist.md       (generated checklist)
  |
  v
Developer copies artifacts into the service repository
  |
  v
Developer opens PR in the service repository with generated workflow files
  |
  v
Platform team reviews and approves
```

## Inputs

| Field | Required | Example |
|-------|----------|---------|
| `repository` | Yes | `my-org/my-repository` |
| `service_name` | Yes | `my-new-api` |
| `service_type` | Yes | `api` / `web` / `worker` / `integration` |
| `runtime` | Yes | `python` / `node` / `static` |
| `gcp_project` | Yes | `cgm-assistant-prod` |
| `region` | Yes | `us-central1` |
| `owner` | Yes | `team-name` |
| `cost_center` | Yes | `cc-code` |
| `environment` | Yes | `prod` / `staging` / `dev` |
| `cloud_run_service_name` | Yes | `my-new-api` |
| `health_path` | No | `/health` |
| `openapi_path` | No | `/openapi.json` |
| `quality_profile` | No | `python`, `node`, or `static`; defaults to runtime |
| `coverage_threshold` | No | Blocking coverage percentage, default `70` |
| `validation_targets` | No | List of external endpoints to smoke-test |

## Generated Artifacts

### gcp-service-release.yaml
The service's release contract, consumed by the platform API for catalog registration.

### Caller Workflows
Thin wrappers that call the platform's reusable workflows with the service's parameters:
```yaml
jobs:
  pr-check:
    uses: diegomad14/gcp-engineering-platform-api/.github/workflows/reusable-pr-check.yml@v1
    with:
      backend-enabled: true
      python-version: "3.11"
```

### Service Labels Manifest
Documents the required GCP labels for cost attribution:
```yaml
labels:
  service: my-new-api
  env: prod
  owner: team-name
  cost_center: cc-code
```

## Workflow

The platform provides `service-onboarding-plan.yml` — a manual `workflow_dispatch` that:
1. Accepts service metadata inputs.
2. Generates all artifacts.
3. Uploads them as workflow artifacts (or opens a PR if token configured).
4. Does NOT create GCP resources, IAM bindings, or Secret Manager entries.

## What the Service Factory Does NOT Do

- Create GCP projects.
- Create Cloud Run services.
- Configure IAM.
- Set up Secret Manager.
- Create Artifact Registry repositories.
- Deploy anything.

These remain manual setup steps by the platform team or service owner.

## Database Decision

**No database for MVP.** The Service Factory generates YAML files and workflow artifacts. No state is persisted between invocations. Phase 2 may add Firestore for an audit trail of generated services.
