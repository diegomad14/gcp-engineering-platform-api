# ADR 0003 - WIF/OIDC, No JSON Keys

## Status

Accepted

## Decision

Use GitHub Actions OIDC with Google Workload Identity Federation for CI/CD authentication to GCP.

Do not use:

- `GCP_SA_KEY`
- `credentials_json`
- Service account JSON files
- Long-lived CI/CD keys

## Minimum GitHub Permissions

```yaml
permissions:
  contents: read
  id-token: write
```

## Runtime Identity

Cloud Run services should run as a dedicated runtime service account. The deployer service account should have `roles/iam.serviceAccountUser` only on the runtime service account it needs to attach.

