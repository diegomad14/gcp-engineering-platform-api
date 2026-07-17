# Service Catalog

The catalog contains independent Cloud Run services. Sharing a repository,
workflow, owner, or version does not create an application hierarchy.

Each entry identifies `service_name`, repository, owner, cost
center, GCP project, region, environment, validation targets, quality metadata,
and FinOps labels. The service name is both the public identifier and the only
user-facing name; the platform does not generate aliases or display names.

Public endpoints:

- `GET /api/catalog/services`
- `GET /api/catalog/services/{service_name}`

The detail endpoint enriches catalog metadata with best-effort Cloud Run URL,
readiness, latest ready revision, and traffic allocation. Catalog data must
never contain secrets, credentials, tokens, database contents, customer data,
or PII.

The aggregate example is `catalog/services.example.yaml`, validated against
`schemas/platform-catalog.schema.json`. Files under `catalog/services/` also
represent one service each; services from the same repository remain separate
files.
