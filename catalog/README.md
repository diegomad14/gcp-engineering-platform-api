# Service Catalog

The catalog captures reusable metadata about services operated on the platform.

Catalog entries should describe ownership, runtime, release gates, rollback targets, and operational risk. They must not contain secrets, database contents, or customer data.

The catalog explicitly separates `managed` resources from `observability_only` inventory identities. Observation-only entries have null repository/owner and verified inventory provenance; all managed operations are blocked, regardless of UI state. The current 51 entries contain 19 managed and 32 observation-only resources, all with logs disabled.

The API derives `deployment_ready` and `deployment_blockers` from each entry.
Services can be visible in the catalog while still blocked from `/deployments`
until repository, workflow, image, Artifact Registry, build context, project,
region, and health-path fields are complete.

## Files

- `services.example.yaml` is the reusable aggregate example.
- `services.schema.json` is a legacy reference to `schemas/platform-catalog.schema.json`, the authoritative contract.
- `services/` contains proposal/reference entries. These YAML files are not loaded at runtime.
- `src/eng_platform_api/static_examples/mock_catalog.json` is the real runtime source shared by catalog metadata and log authorization.

Register a generated proposal locally with `python scripts/catalog_registry.py --check <entry.yaml>`; after review use `--write` for an atomic append to the runtime JSON. New entries keep logs disabled with no readers. Names are globally unique across Services/Jobs, projects and regions. Registration creates no Cloud Run resource or deployment.

See `docs/architecture/service-catalog.md` for field guidance and repository boundaries.
