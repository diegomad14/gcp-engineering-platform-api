# Service Factory onboarding

The Factory creates reviewable local proposals for Cloud Run Services and Jobs.
It does not register entries automatically, create resources, change IAM, push
commits or deploy. Its API and CLI share the same generator.

## Inputs and identity

Required inputs are `repository`, `service_name`, `service_type` (`api`, `web`,
`worker`, `integration`), `runtime` (`python`, `node`, `static`), `gcp_project` and
`owner`. `runtime_kind` selects `cloud_run_service` (compatible default) or
`cloud_run_job`; generated entries always include the kind explicitly.

`service_name` is the actual Cloud Run runtime ID and must be globally unique,
even across projects, regions and kinds. `cloud_run_service_name` is a deprecated
image-name override only; it never changes the runtime identity. `region`
defaults to `us-central1`, environment to `prod`, and coverage to 70%. The
blocking quality policy is `oss-v2`, with changed-line threshold 80%.

The API contract is `schemas/service-factory-request.schema.json`. The CLI also
accepts `--app-name` for a fallback repository name. Use explicit `--repository`
when the repository already exists.

## Generate and register

From the API repository with its Python dependencies installed:

```bash
python scripts/service_factory.py \
  --app-name example --repository my-org/example \
  --service-name example-batch-job --service-type worker --runtime python \
  --runtime-kind cloud_run_job --gcp-project my-project \
  --owner platform --cost-center engineering --output-dir /tmp/example-proposal

python scripts/catalog_registry.py --check \
  /tmp/example-proposal/catalog/services/example-batch-job.yaml

# Run only after reviewing the proposal and intended local JSON diff:
python scripts/catalog_registry.py --write \
  /tmp/example-proposal/catalog/services/example-batch-job.yaml
```

Factory output defaults to `service-factory-proposal/`, avoiding installation of
workflows in the current repository. Review existing files before choosing an
output directory. Register the generated `catalog/services/<name>.yaml`, not the
release contract. `catalog_registry.py` defaults to a dry run; `--write` atomically
appends to the actual source,
`src/eng_platform_api/static_examples/mock_catalog.json`. A separate existing
JSON file can be selected with `--catalog` for local tests. Duplicate names are
rejected; this command does not update or replace existing entries.

Proposed YAML under `catalog/services/` is not loaded at runtime. Review the JSON
diff and deliver it through the normal platform release process. Then verify the
entry in `/api/catalog/services`; no per-resource code or manual allowlist is
needed. A successful local registration is not a deployment or proof of a live
Cloud Run resource.

## Generated artifacts

Both `gcp-service-release.yaml` and `gcp-job-release.yaml` validate against
`schemas/gcp-service-release.schema.json`. This shared proposal schema requires
an explicit `release_target.runtime_kind`; `release_target.platform` remains
`gcp-cloud-run` for both kinds. It rejects Job HTTP validation and requires a
Job-only deployment guard with `enabled: false`, `executor: cloud_build` and a
nonempty review requirement. Schema validity is not deployment authorization.
Older release proposals without `runtime_kind` need the actual kind filled in
before validation against the current schema.

Both kinds include CI/quality and semantic-release workflow proposals, catalog
entry, `.quality-gate.yml`, quality-source inventory, labels, checklist and handoff
prompt. All new entries have logs disabled and an empty reader list. The importer
rejects any input that attempts to grant log access; readers require a separate
reviewed catalog-policy change.

Services additionally generate `gcp-service-release.yaml` with HTTP validation
and proposed `platform-deploy.yml`/`platform-rollback.yml`. Candidate/promote/
rollback API output remains available only for Services. These artifacts require
review of existing authorization, protected checks and release ownership before
adoption.

Jobs generate `gcp-job-release.yaml` and `cloud-run-job-labels.yaml`, with
`deployment.enabled: false` and `executor: cloud_build`. No Service deployment,
promotion or rollback workflow, HTTP endpoint, OpenAPI path or traffic contract
is generated. Review the Job executor and job-specific release policy before
separately enabling deployment. Artifact Registry, credentials, runtime accounts,
Job existence and execution policy remain explicit review prerequisites.

See [the catalog contract and registration safeguards](../architecture/service-catalog.md).

## Inventory-only identities

An existing `management_mode: observability_only` identity cannot be targeted by
Factory generation: the API/CLI rejects its `service_name` before producing
deployment artifacts. Runtime names are globally unique. `cloud_run_service_name`
is only an image override and cannot be used to retarget an inventory identity.
Adoption requires a separately reviewed catalog change using verified ownership
and repository metadata. Registering inventory metadata or enabling logs does
not grant deployment, rollback, release or secret capabilities.
