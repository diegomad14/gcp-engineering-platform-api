# Engineering Platform API

FastAPI control plane for independent Cloud Run services.

## New here?

See [`docs/onboarding/new-developer-guide.md`](docs/onboarding/new-developer-guide.md)
for full setup, the contribution checklist, an architecture map and a
troubleshooting runbook. Quick checklist:
[`docs/checklists/new-developer-onboarding.md`](docs/checklists/new-developer-onboarding.md).

## Local development

```bash
pip install -e ".[dev]"
export ENG_PLATFORM_MOCK_MODE=true
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
| GET | `/api/auth/me` | Current GitHub operator session and deploy permission |
| GET | `/api/auth/login` | Start GitHub OAuth with a protected return URL |
| GET | `/api/auth/callback` | Complete GitHub OAuth and create the signed session |
| POST | `/api/auth/logout` | Clear the operator session |
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
- `GCP_RELEASE_WIF_PROVIDER`: dedicated WIF provider restricted to protected
  release tags and the platform deploy/rollback workflows. Keep the older
  `GCP_WIF_PROVIDER` only for read-only CI validation.
- `ENG_PLATFORM_DEPLOYMENT_FIRESTORE_COLLECTION` enables durable minimal
  metadata; local development falls back to `data/deployments.json`.

The GitHub App installation needs **Actions: read/write**, **Contents: read** and
**Deployments: read/write**. The API creates one GitHub Deployment per service
and correlates the exact Actions run from the Deployment status `log_url`.
Only one non-terminal deployment may run for a service at a time.

Normal service release workflows require a short-lived authorization issued by
this API. The API signs it with `ENG_PLATFORM_RELEASE_SIGNING_PRIVATE_KEY`; the
service workflow verifies the public key stored in the repository variable
`ENG_PLATFORM_RELEASE_SIGNING_PUBLIC_KEY` and consumes the authorization once
through `/api/internal/release-authorizations/consume`. Direct workflow
dispatches without a valid platform authorization fail before WIF/GCP access.

Each catalog service owns its deployment coordinates, even when multiple
services share a repository:

```yaml
name: cgm-sanplat-web
repository: diegomad14/parametrizacion-correos-cgm
deployment:
  enabled: true
  workflow_file: platform-deploy.yml
  image_name: cgm-sanplat-web
  artifact_repository: cgm-sanplat-repo
  build_context: frontend
  health_path: /
```

GitHub remains authoritative for tags, workflow state, jobs and logs.

## Operator authentication

- `ENG_PLATFORM_GITHUB_OAUTH_CLIENT_ID` and
  `ENG_PLATFORM_GITHUB_OAUTH_CLIENT_SECRET`: GitHub OAuth App credentials.
- `ENG_PLATFORM_SESSION_SECRET`: long random value used to sign the session
  cookie; store it in Secret Manager.
- `ENG_PLATFORM_FRONTEND_URL`: exact allowed Web origin and OAuth return origin.
- `ENG_PLATFORM_ALLOWED_GITHUB_LOGINS`: comma-separated GitHub logins allowed to
  deploy; defaults to `diegomad14`.
- `ENG_PLATFORM_TRUST_IAP_IDENTITY`: must remain `false` unless the API is
  exclusively behind a verified IAP boundary. Public deploy authorization uses
  GitHub OAuth sessions only.
- `ENG_PLATFORM_RELEASE_SIGNING_PRIVATE_KEY`: Ed25519 private key stored only
  through Secret Manager in production.
- `ENG_PLATFORM_RELEASE_AUTH_FIRESTORE_COLLECTION`: durable one-time ticket
  collection; configure this in production so multi-instance Cloud Run cannot
  replay a release authorization.

Register the OAuth callback as
`https://<api-host>/api/auth/callback`. The browser session stores only the
GitHub login and avatar URL; the OAuth access token is not persisted. Deploy
writes require an authenticated, allowlisted operator. Mock mode signs in as
`diegomad14` for local and end-to-end tests.

## SonarCloud and coverage

CI writes Python coverage in Cobertura XML (`coverage.xml`) before the pinned
SonarQube scanner runs. `sonar-project.properties` imports that report and waits
for the Quality Gate, so a red Sonar result fails `quality` and blocks merge.
Agents must additionally run `python scripts/quality/sonar_agent_check.py
--pull-request <PR_NUMBER>` before completing PR work.

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
