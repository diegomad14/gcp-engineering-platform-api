# Service Catalog

The catalog captures reusable metadata about services operated on the platform.

Catalog entries should describe ownership, runtime, release gates, rollback targets, and operational risk. They must not contain secrets, database contents, or customer data.

The API derives `deployment_ready` and `deployment_blockers` from each entry.
Services can be visible in the catalog while still blocked from `/deployments`
until repository, workflow, image, Artifact Registry, build context, project,
region, and health-path fields are complete.

## Files

- `services.example.yaml` is the reusable aggregate example.
- `services.schema.json` is the baseline schema for aggregate service catalog files.
- `services/` contains service-specific entries retained for current platform services.

See `docs/architecture/service-catalog.md` for field guidance and repository boundaries.
