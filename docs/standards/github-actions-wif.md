# GitHub Actions WIF Standard

## Required Repository Variables

- `GCP_WIF_PROVIDER`
- `GCP_WIF_SERVICE_ACCOUNT`
- `GCP_PROJECT_ID`
- `GCP_REGION`

## Runner selection

Release, deploy and rollback workflows resolve their runner from the optional
`runner_label` input and then the repository variable `CGM_ACTIONS_RUNNER`.
Only the exact label `cgm-release-local` is accepted as a contingency value;
all other values resolve to `ubuntu-latest`:

```yaml
runs-on: ${{ (inputs.runner_label == 'cgm-release-local' || (inputs.runner_label == '' && vars.CGM_ACTIONS_RUNNER == 'cgm-release-local')) && 'cgm-release-local' || 'ubuntu-latest' }}
```

`ubuntu-latest` is the normal value. The local contingency is a repository-
scoped runner inside a disposable Linux x64 VM launched from the developer's
Windows, macOS or Linux machine. Pull-request, PR-title and Sonar/quality
workflows remain on hosted runners and must not execute on the local runner.
Restore the hosted runner after the incident. Follow the canonical Obsidian
runbook `Runbook - GitHub Actions bloqueado` for billing, quota, runner and
break-glass decisions.

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
- Self-hosted runners shared across untrusted repositories without isolation
- Long-lived GCP credentials installed on a GitHub runner
- Direct self-hosted runners installed on a developer's host
- Arbitrary values for `CGM_ACTIONS_RUNNER` or `runner_label`
