# Service Template Adoption Model

## Principle

The platform repository defines reusable patterns. It does not execute production releases for application services.

Application repositories adopt selected templates through controlled PRs. There is no auto-sync, no blind copy-paste, and no production traffic movement from this repository.

## Sources of Truth

- Platform repository: source of truth for reusable patterns, standards, runbooks, and template baselines.
- Application repository: source of truth for service code, service-specific workflows, Dockerfiles, tests, and runtime configuration.
- Obsidian or operational wiki: source of truth for incidents, epics, product state, release decisions, and live operational status.

## Adoption Flow

```text
Platform template update
  |
  v
Review in platform repo
  |
  v
Approved reference baseline
  |
  v
Service adoption plan
  |
  v
Small PR in app repo
  |
  v
Dry-run / no-traffic candidate
  |
  v
Manual validation
  |
  v
Manual promote
```

## Adoption Rules

- Services must adapt inputs and gates to their own runtime.
- Services must validate WIF/OIDC before release adoption.
- Services must validate release candidate, promote, and rollback independently.
- Services must keep behavior-level gates for their critical user flows.
- Services must not adopt templates by copying them without review.
- Services must not remove existing production safety gates during adoption.

## No Auto-Sync

Template updates do not automatically update app repositories. Each app repository adopts a platform template version through a normal PR with review and validation.

## Rollback Compatibility

Promote and rollback workflows must preserve API and Web compatibility. If a service has coupled API/Web behavior, adoption must document whether API and Web are promoted together or independently.

## Adoption Evidence

Every adoption PR should include:

- Template source version or platform commit.
- Scope of adopted workflows.
- Dry-run evidence.
- Candidate no-traffic validation evidence.
- Rollback commands.
- Known limitations.
