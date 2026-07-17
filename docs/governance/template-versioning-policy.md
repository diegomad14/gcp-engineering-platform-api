# Template Versioning Policy

## Purpose

Template versioning makes platform changes auditable and gives service owners a clear adoption target.

## Change Types

### Docs-Only

Documentation-only changes do not require service adoption review unless they change operational instructions for production releases.

### Non-Breaking Template Change

Examples:

- Additional comments.
- Optional inputs with safe defaults.
- Additional validation that does not change existing required inputs.
- Runbook clarification.

Non-breaking changes require normal platform PR review.

### Breaking Template Change

Examples:

- Required input renamed or removed.
- Workflow permission changes.
- Runtime identity assumptions changed.
- Candidate, promote, or rollback semantics changed.
- New required production gate.
- Change that affects rollback behavior.

Breaking changes require:

- Platform review.
- Security review.
- Release engineering review.
- Release notes.
- Service adoption review before use by any app repository.

## Tags

Use repository tags for platform template releases only after an explicit platform decision. Do not create product release tags from this repository.

Until platform template tags are approved, services should reference platform commit SHAs or PR numbers in their adoption PRs.

## Release Notes

Template release notes should include:

- Changed templates.
- Required inputs.
- New gates.
- Breaking changes.
- Migration guidance.
- Known limitations.

## Service Notification

When a template changes in a way that affects service adoption, notify service owners through the service catalog owner field, the application repository issue tracker, or the operational wiki.

## Adoption Review Requirement

Any service PR adopting or upgrading release, promote, rollback, WIF, or data-source templates must link to:

- The platform PR or commit being adopted.
- The service readiness checklist.
- The rollback plan.
- Validation evidence.
