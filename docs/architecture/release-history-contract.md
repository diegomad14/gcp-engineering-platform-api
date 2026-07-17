# Release History Contract

## Decision

Release history is service-oriented. Every public `ReleaseItem` represents one
Cloud Run service, even when multiple services were deployed by the same
repository workflow.

The webhook accepts a repository-level event with `services[]` and persists one
row for each entry:

```json
{
  "repository": "diegomad14/parametrizacion-correos-cgm",
  "version": "v0.9.56",
  "status": "promoted",
  "services": [
    {
      "service_name": "cgm-sanplat-api",
      "revision": "cgm-sanplat-api-00006-jiw",
      "action": "promoted"
    }
  ],
  "github_run_url": "https://github.com/example/repo/actions/runs/123"
}
```

Responses expose flat rows with `service_name`, `repository`, `version`,
`status`, `revision`, `action`, `github_run_url`, and `created_at`.

## Endpoints

- `POST /api/releases`
- `GET /api/releases?service_name=<name>`
- `GET /api/releases/summary`
- `GET /api/releases/{service_name}/latest`

## Semantics and Compatibility

Actions are `promoted`, `deployed`, `rolled_back`, `unchanged`,
`not_included`, or `missing`. An empty revision paired with a deployment action
is normalized to `missing`.

This is a breaking `v0.4.0` contract. Public `app_id`, `app_name`,
`api_revision`, and `web_revision` fields are removed and legacy request fields
are rejected. Existing grouped JSON records are split into service rows while
reading; records whose services cannot be associated with a catalog repository
are discarded.

GitHub-discovered runs produce a `missing` row for every catalog service in the
repository. Summary deduplication uses `github_run_url + service_name`, and the
total counts the final merged service rows.
