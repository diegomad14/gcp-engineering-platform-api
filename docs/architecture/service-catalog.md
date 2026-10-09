# Service and Job catalog

Production runtime inventory and log-reader policies use an explicitly configured,
SHA-256-pinned private catalog. Catalog metadata and log authorization read the
same validated snapshot through `services/log_catalog.py`. The checked-in
`src/eng_platform_api/static_examples/mock_catalog.json` contains disabled public
examples, including synthetic observation-only fixtures. It is not production
inventory or a fallback when a configured private authority is unavailable.
Files under `catalog/services/` and `catalog/services.example.yaml` are proposals;
they are not discovered or loaded at runtime.

Every entry has explicit `service_name`, `project_id`, region and
`deployment.runtime_kind` (`cloud_run_service` or `cloud_run_job`). A
`service_name` is globally unique, including across projects, regions and kinds,
because existing public routes address resources by that name. Shared images,
repositories and owners do not create aliases or an application hierarchy.
`deployment.image_name` is an image identifier, never the Cloud Run runtime name.

The entire source must validate before either metadata or log authorization can
use it. Missing/corrupt files, duplicate names or JSON keys, unknown fields and
malformed policies fail closed. Bounds are 4 MiB, 2,048 resources and 2,048 distinct
projects. No partial or previous snapshot is used as a fallback. The formal
contract is `schemas/platform-catalog.schema.json`; the legacy
`catalog/services.schema.json` is only a reference to that authoritative schema.

## Management modes

`management_mode` is the explicit discriminator:

- `managed`: repository and owner are known and non-null. Existing deployment,
  release and authorization policies continue to apply; the mode alone does not
  grant permission to mutate a resource.
- `observability_only`: repository and owner are explicitly `null`. A verified
  inventory origin and timestamp are mandatory in `inventory_source`. Deployment
  and quality are disabled, secret bindings and validation targets are empty,
  and no image, workflow, environment, owner or cost center is inferred.

The public examples contain 51 resources: 19 previously public managed entries
and 32 synthetic observation-only entries. Every example has logs disabled and
an empty reader list. The synthetic project, demo names and 2020 timestamp are
fixtures only. Runtime reader approval belongs exclusively to the private
catalog. The global feature remains OFF and its reader allowlist empty by
default; new resources remain disabled with no readers. No policy verifies
production activation, connectivity or IAM. The public DTO exposes the mode and
nullable metadata. An observation-only detail stays `status=unknown` without a
Cloud Run/readiness lookup or fabricated mock health. Managed release/quality
projections exclude observation-only resources.

Observation-only resources are blocked server-side before provider or store
mutations, including deployment, rollback, release registration/execution,
secret changes and Factory adoption proposals. Existing operation callbacks
must re-check the current authority. These barriers are independent of UI buttons
and `deployment_ready`. An explicitly approved private log-reader policy allows observation only
when the independent global checks pass, without granting any managed capability.

The local importer never replaces an existing identity or upgrades its mode.
Adoption of an existing observation-only resource requires a separately reviewed
catalog change with verified ownership, repository and managed configuration;
it cannot be inferred from a name or bypassed through Factory generation.

## Register a local Factory or inventory proposal

From the API repository with its dependencies installed:

```bash
python scripts/catalog_registry.py --check /path/to/catalog/services/new-resource.yaml
python scripts/catalog_registry.py --write /path/to/catalog/services/new-resource.yaml
```

Omitting `--check`/`--write` performs the same read-only check. `--write` validates
the complete resulting catalog and appends using an atomic local replacement;
it never replaces an existing resource. Cooperating CLI writers are serialized
using a local POSIX lock; an observed concurrent edit aborts registration. Use
`--catalog /path/to/test-catalog.json` for a separate existing local JSON catalog.
The input is one catalog-entry YAML, not the release contract. Factory proposals
are managed. An observation-only proposal must explicitly carry null ownership
and its verified inventory provenance, and validate the observation-only branch.
The command summarizes the resulting total, Service/Job counts, project count and
number added plus managed/observation-only counts without printing reader identities.

Review the resulting JSON diff through the normal repository/release process.
The CLI does not push, deploy, contact GCP, change IAM, or create any runtime
resource. Once the reviewed catalog is delivered, new entries are available
without changing Python allowlists or discovery code.

## Logs and Job safety

New entries always contain `logs.enabled: false` and `logs.allowed_logins: []`.
The importer rejects proposals that enable logs, provide readers, or contain a
malformed policy. An omitted block is registered as explicitly disabled. Granting
log access requires a separate reviewed change to the authoritative catalog;
registration never silently grants it. Existing policies remain unchanged.

Factory Job proposals use `deployment.enabled: false` and `executor: cloud_build`.
Review the Job executor and job-specific release policy before enabling them.
They contain no Service deploy/promote/rollback workflows or HTTP health/OpenAPI
contract. Neither catalog membership nor successful registration proves a Job or
Service exists, is ready, or is deployed.

Public metadata endpoints are `GET /api/catalog/services` and
`GET /api/catalog/services/{service_name}`. Managed detail may enrich metadata with
best-effort Cloud Run state; observation-only detail remains metadata only. Log reader lists stay private. Catalog files must
never contain credentials, tokens, customer data or secret values.

## Auxiliary infrastructure references

Managed entries may include `infrastructure_resources`, a bounded list of typed
GCP identity metadata with `resource_type`, `resource_name` and optional
`description`. Supported kinds are Firestore databases, GCS buckets, Cloud Tasks
queues, Scheduler jobs, Artifact Registry repositories, Secret Manager secrets
and Billing budgets. Secret references contain names only; operational secret
bindings remain in `operational_secrets`.

These references describe dependencies of one managed Service/Job. They are not
additional catalog services, deployment targets, log authorities, readiness
checks, cloud discovery or permission grants. Existing Cloud Run coordinates and
reader policies alone continue to define those capabilities. Observation-only
entries cannot carry managed auxiliary references. Omitted or empty references
preserve existing API payloads and authority fingerprints; nonempty references
are returned as metadata without querying GCP. Actual resource existence and
ownership must be verified during the separate infrastructure review.
