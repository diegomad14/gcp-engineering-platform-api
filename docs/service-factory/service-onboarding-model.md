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
  ├── platform-deploy.yml           (GitHub-native deploy entrypoint)
  ├── platform-rollback.yml         (GitHub-native rollback entrypoint)
  ├── semantic-release.yml          (immutable tag generation)
  ├── ci.yml                        (quality gate)
  ├── catalog/services/<svc>.yaml   (platform catalog entry)
  ├── .quality-gate.yml             (open source quality policy)
  ├── cloud-run-service-labels.yaml (label manifest)
  ├── onboarding-checklist.md       (generated checklist)
  └── agent-handoff-prompt.md       (copyable prompt for PR-ready adoption)
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

### Platform Workflows
`platform-deploy.yml` and `platform-rollback.yml` expose the
`workflow_dispatch` interface called by Engineering Platform `/deployments`.
Developers should not call these workflows directly with `gh workflow run`.

### Agent Handoff Prompt
The prompt tells Codex/Claude how to create PR-ready adoption changes while
forbidding secrets, service account JSON, GCP Console deploys, direct
`gh workflow run`, and manual `gcloud run deploy`.

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
3. Shows them in the UI for copy/paste or agent-assisted PR creation.
4. Does NOT create GCP resources, IAM bindings, or Secret Manager entries.
5. Does NOT open PRs or deploy production in the current iteration.

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
