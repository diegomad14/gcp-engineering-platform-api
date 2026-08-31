# Parametrizacion Correos CGM Adoption Plan

## Service

- App repo: `diegomad14/parametrizacion-correos-cgm`
- GCP project: `cgm-assistant-prod`
- Region: `us-central1`
- API service: `cgm-sanplat-api`
- Web service: `cgm-sanplat-web`

## Current State

- Phase 1 recovery: closed
- API revision: `cgm-sanplat-api-00179-tad`
- Web revision: `cgm-sanplat-web-00080-g8c`
- OpenAPI paths: 77
- Perseo: recovered
- Rollback: documented

## Blockers

- Credential rotation confirmation pending
- Connection pool exhausted tracked as P1
- App adoption implementation blocked

## Recommended Plan

1. Inventory current app repository workflows and remove custom release logic in a dedicated app repo PR.
2. Adopt caller workflows from `examples/caller-workflows/` pinned to `diegomad14/gcp-engineering-platform-api@v0.15.0`.
3. Configure repository variables: `APP_ID=cgm-integration-platform`, `APP_NAME=CGM Integration Platform`, `GCP_PROJECT_ID`, `GCP_REGION`, `GCP_WIF_PROVIDER`, `GCP_WIF_SERVICE_ACCOUNT`, `API_SERVICE_NAME`, `WEB_SERVICE_NAME`, `ARTIFACT_REPO`, image names, URLs, `RUNTIME_SERVICE_ACCOUNT`, and optional `PLATFORM_API_URL`.
4. Migrate `release-candidate` first and validate no-traffic candidate behavior.
5. Migrate `promote-prod` after candidate validation; keep strict traffic/revision checks and treat post-promote CORS/OpenAPI/Web diagnostics as warnings.
6. Add `promote-emergency` for incident-only traffic movement with confirmation `EMERGENCY_PROMOTE`.
7. Migrate `rollback-prod` last and verify dynamic rollback revision discovery before relying on it in an incident.

## Caller Workflow Mapping

| App repo workflow | Platform caller example |
|---|---|
| PR checks | `examples/caller-workflows/pr-check-caller.yml` |
| SonarQube | `examples/caller-workflows/sonarqube-caller.yml` |
| Release candidate | `examples/caller-workflows/release-candidate-caller.yml` |
| Promote PROD | `examples/caller-workflows/promote-caller.yml` |
| Emergency promote | `examples/caller-workflows/promote-emergency-caller.yml` |
| Rollback PROD | `examples/caller-workflows/rollback-caller.yml` |

## Gates

- Safe to plan: YES
- Safe to implement: NO
- Safe to deploy: NO

## Notes

This plan does not modify the app repository. It documents the controlled adoption path that should be followed in a future app PR.
