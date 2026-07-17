# GitHub Actions WIF Standard

## Required Repository Variables

- `GCP_WIF_PROVIDER`
- `GCP_WIF_SERVICE_ACCOUNT`
- `GCP_PROJECT_ID`
- `GCP_REGION`

## Required Workflow Shape

```yaml
permissions:
  contents: read
  id-token: write
```

Every job that uses `google-github-actions/auth` should checkout the repository first unless there is a documented reason not to.

Every job that executes `gcloud` should install `google-github-actions/setup-gcloud`.

## Disallowed Patterns

- `GCP_SA_KEY`
- `credentials_json`
- Default compute service account as runtime identity
- Deploying `latest` to production
- Tag regex written as invalid GitHub glob

