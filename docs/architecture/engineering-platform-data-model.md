# Engineering Platform Data Model

## Decision: No Database for MVP

The platform does not require its own database for MVP. Data is sourced from existing systems:

| Data Domain | Source | Access Pattern |
|-------------|--------|---------------|
| Service catalog | YAML files in GitHub | Read from repo, served by API |
| Release history | GitHub Actions run history | GitHub API |
| Cost data | BigQuery Billing Export | Read-only SQL queries |
| Operational metrics | Cloud Monitoring | Read-only Monitoring API |
| Quality data | CI quality reports | Platform API + private GCS objects |
| Service configs | YAML contracts in service repositories | Read from repo |

## Why No Database

1. **Platform is read-only** for external data — it surfaces what already exists.
2. **Source of truth** is already established: GitHub (config), BigQuery (costs), Cloud Monitoring (metrics).
3. **Reducing operational surface** — no database means no backups, no migrations, no connection strings, no secret management.
4. **Cloud Run min-instances=0** — a database would require an always-on connection, defeating the scale-to-zero design.

## Phase 2: Optional Firestore

If the platform needs to cache data, store UI preferences, or maintain an audit log:

- **Firestore in Datastore mode** — serverless, scales to zero, no connection management.
- Use cases: UI preferences, query cache TTL, service factory audit trail.
- Still optional — the platform functions without it.

## Data Flow

```
BigQuery Billing Export
  |
  v
Platform API (read-only, mock-backed)
  |
  v
Platform Web UI (displays cost data)

Cloud Monitoring API
  |
  v
Platform API (read-only, mock-backed)
  |
  v
Platform Web UI (displays metrics)

GitHub Repos (YAML contracts)
  |
  v
Platform API (reads catalog, releases)
  |
  v
Platform Web UI (displays catalog, releases)
```
