# ADR 0002 - Candidate Promote Release Model

## Status

Accepted

## Context

A release can appear successful while Cloud Run traffic remains pinned to an older revision. Validating only the service URL can hide this because the service URL may still route to the previous stable revision.

## Decision

Use a two-step model:

1. Build and deploy candidate revisions with no production traffic.
2. Assign revision tags and validate candidate URLs.
3. Promote traffic manually after pre-promotion validation.
4. Smoke production service URLs after promotion.
5. Print rollback commands with captured previous revisions.

## Required Gates

- SemVer tag validation.
- Candidate revision exists and is Ready.
- Tagged URL belongs to the expected revision.
- API health check on tagged URL.
- OpenAPI path count and critical path validation.
- Web HEAD on tagged URL.
- CORS preflight validation.
- Post-promotion active revision verification.

