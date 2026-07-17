# ADR 0001 - Platform Repository Scope

## Status

Accepted

## Context

The source application repository contains product code, production workflows, incident history, and service-specific docs. Several recovery artifacts are reusable across other GCP services, but moving product code into a platform repository would create ownership and release risk.

## Decision

Create a separate reusable platform repository containing templates, runbooks, standards, examples, scripts, and catalog metadata. Keep product code and production workflows in the app repositories until each service explicitly adopts a platform template.

## Consequences

- Platform assets can evolve independently.
- App repositories retain service-specific ownership and production safety.
- Adoption happens through explicit PRs into each service repository.

