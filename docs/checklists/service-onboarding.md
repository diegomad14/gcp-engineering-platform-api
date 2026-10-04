# Service and Job onboarding checklist

- [ ] Identify owner, repository, project, region and explicit runtime kind.
- [ ] Choose a globally unique `service_name`, also across projects/kinds.
- [ ] Confirm the runtime exists separately; Factory artifacts do not create it.
- [ ] Review required labels, accounts, Artifact Registry and existing authorization.
- [ ] Generate proposals outside production workflow directories.
- [ ] Review the blocking `oss-v2` quality gate and exact SHA/release identity.
- [ ] Services: review HTTP health checks and proposed deploy/rollback workflows.
- [ ] Jobs: retain `deployment.enabled: false`, use `executor: cloud_build`, and
      review the Job executor and job-specific policy; do not adopt Service
      deploy/promote/rollback workflows or HTTP validation.
- [ ] Run `python scripts/catalog_registry.py --check <generated-entry.yaml>`.
- [ ] After review run `python scripts/catalog_registry.py --write <generated-entry.yaml>`.
- [ ] Review the diff to `src/eng_platform_api/static_examples/mock_catalog.json`;
      `catalog/services/*.yaml` is not a runtime source.
- [ ] Preserve `logs.enabled: false` and `logs.allowed_logins: []`; review any log
      reader grant as a separate catalog-policy change.
- [ ] Deliver the reviewed catalog through the normal release process and verify
      `/api/catalog/services` shows the new entry without code or allowlist edits.
- [ ] Document secret inventory without values, data policy and operational runbooks.
