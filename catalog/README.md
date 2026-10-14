# Service Catalog

The catalog captures reusable metadata about services operated on the platform.

Catalog entries should describe ownership, runtime, release gates, rollback targets, and operational risk. They must not contain secrets, database contents, or customer data.

The catalog explicitly separates `managed` resources from `observability_only` inventory identities. Observation-only entries have null repository/owner and verified inventory provenance; all managed operations are blocked, regardless of UI state. The checked-in examples are fixtures, not a production inventory. Read the validated private production catalog to report current managed and observation-only counts.

The API derives `deployment_ready` and `deployment_blockers` from each entry.
Services can be visible in the catalog while still blocked from `/deployments`
until repository, workflow, image, Artifact Registry, build context, project,
region, and health-path fields are complete.

## Files

- `services.example.yaml` is the reusable aggregate example.
- `services.schema.json` is a legacy reference to `schemas/platform-catalog.schema.json`, the authoritative contract.
- `services/` contains proposal/reference entries. These YAML files are not loaded at runtime.
- `src/eng_platform_api/static_examples/mock_catalog.json` contains public disabled fixtures. Production metadata and log authorization share the private, SHA-256-pinned catalog configured on the runtime.

Register a generated proposal locally with `python scripts/catalog_registry.py --check <entry.yaml>`; after review use `--write` for an atomic append to the selected local catalog JSON. Publishing the reviewed private production snapshot is a separate release step. New entries keep logs disabled with no readers. Names are globally unique across Services/Jobs, projects and regions. Registration creates no Cloud Run resource or deployment.

See `docs/architecture/service-catalog.md` for field guidance and repository boundaries.

Managed services default release and deployment to GitHub Actions. Legacy
`release.executor: null` and `deployment.executor: auto` also resolve to GitHub;
account quota and runtime fallback settings cannot change that choice. An explicit
per-service executor or operator `provider` may choose Cloud Build, subject to its
readiness and new-build policy. Provider selection is locked when an operation is
reserved. Observation-only resources remain blocked, and disabled Job proposals
are not enabled by default changes.
