# Cloud Run Service Standard

## Identity

- Use a dedicated runtime service account per service or service family.
- Do not run production services as the default compute service account.
- Deploy using WIF/OIDC from GitHub Actions.

## Traffic

- Release candidates must deploy with no traffic.
- Production traffic movement must be explicit and auditable.
- Rollbacks should use `gcloud run services update-traffic`, not `gcloud run deploy`.

## Configuration

- Use Secret Manager references for secrets.
- Use merge update strategy when supported by deployment tooling.
- Do not remove existing environment variables unintentionally.
- Document any legacy env var to secret-reference migration.

## Health Gates

- API health endpoint.
- Web HEAD endpoint.
- OpenAPI or contract inventory when applicable.
- CORS preflight when a browser client exists.
- Behavior-level smoke tests for known critical flows.

## Private Data Sources

- Keep sensitive data files outside Git.
- Prefer private GCS buckets with uniform bucket-level access for file-based data sources.
- Mount Cloud Storage volumes read-only.
- Grant only the runtime service account the minimum read role required, normally `roles/storage.objectViewer`.
- Validate data-source availability in a no-traffic candidate revision before promotion.
- Do not print data contents, PII, or credentials in workflow logs.

See `docs/runbooks/gcs-private-data-source.md` for the reusable pattern.
