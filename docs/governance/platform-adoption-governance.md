# Platform Adoption Governance

## Purpose

This governance defines how a service can adopt reusable GCP Engineering Platform templates in a controlled, auditable, and reversible way.

The platform repository provides reference baselines. It does not deploy production services directly and does not own service implementation code.

## Who Can Adopt Templates

A service can start adoption planning when it has:

- A named service owner.
- An identified application repository.
- Identified Cloud Run services and GCP project.
- A dedicated runtime service account.
- A deployer service account using WIF/OIDC.
- Documented rollback revisions.
- A reviewed readiness checklist.

Adoption implementation requires approval from the service owner, platform reviewer, and release owner.

## Required Reviews

Each adoption PR must receive:

- Platform review for template correctness.
- Security review for identity, secret, and data boundaries.
- Service owner review for runtime behavior.
- Release engineering review for candidate, promote, and rollback paths.

## Allowed Changes

Adoption PRs may:

- Copy selected template patterns into an application repository.
- Replace placeholders with service-specific non-secret values.
- Add service-specific smoke and behavior gates.
- Add docs, runbooks, and rollback commands.
- Add dry-run or no-traffic candidate validation.

## Prohibited Changes

Adoption PRs must not:

- Deploy directly to production.
- Move Cloud Run traffic without explicit manual promotion.
- Enable auto-promote.
- Commit secrets, `.env` files, database files, dumps, CSV exports, or PII.
- Use service account JSON keys.
- Use `GCP_SA_KEY`.
- Modify Secret Manager, IAM, or GitHub secrets without a separate approved change.
- Copy app code into the platform repository.

## Approval Model

Adoption moves through five gates:

1. Planning.
2. Dry-run.
3. Candidate no-traffic.
4. Manual promote.
5. Rollback readiness.

No stage may be skipped for a production service.

## Stage Gates

### Planning

- Service catalog entry exists.
- Readiness checklist completed.
- Existing app workflows inventoried.
- Rollback revision documented.
- Security blockers identified.

### Dry-Run

- App PR is scoped to docs or inert workflow changes.
- `actionlint` and YAML validation pass.
- No GCP mutation is executed.
- No production traffic changes.

### Candidate No-Traffic

- Candidate revision deploys with no production traffic.
- Candidate revision tag URL is validated.
- API health, OpenAPI count, CORS, Web HEAD, and behavior smoke pass as applicable.
- Candidate report includes revisions and image digests.

### Manual Promote

- Operator supplies explicit confirmation.
- Expected current revisions match live state.
- Target revisions are Ready.
- Rollback commands are printed before or during promotion.
- Post-promotion smoke validates service URLs.

### Rollback Readiness

- Known-good rollback revisions are preserved.
- Rollback workflow supports API and Web as needed.
- Rollback smoke commands are documented.

## Change Rules

- Use small PRs per service.
- Do not combine app workflow adoption with feature delivery.
- Do not migrate multiple services in one PR.
- Do not directly edit production resources from the platform repository.
- Keep adoption evidence in PR descriptions and service docs.

## Rollback Policy

Every service adoption must document:

- Immediate rollback revision for API.
- Immediate rollback revision for Web when applicable.
- Historical manual-good fallback when available.
- Exact `gcloud run services update-traffic` commands.
- Smoke commands after rollback.

## Breaking Change Policy

A template change is breaking when it changes required inputs, workflow permissions, runtime identity assumptions, promotion semantics, rollback semantics, or validation gates.

Breaking changes require:

- Platform ADR or governance note.
- Release notes.
- Explicit adoption review per affected service.
- No automatic propagation to app repositories.

## PR Requirement

Each service adoption must be a small, reviewable PR in the application repository. The platform repository remains the pattern source of truth; the application repository remains the service implementation source of truth.
