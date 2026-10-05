# Remote MCP for eng-platform

The Streamable HTTP endpoint `/mcp` uses the existing release command engine and
BD capture engine. Any authenticated GitHub account that explicitly consents to
`eng-platform.access` can use every MCP action over enabled databases and
registered services. There are no MCP reader/deployer roles or GitHub allowlists.
SQL remains read-only. Web/REST operator and database allowlists are unchanged.

## Activation and OAuth migration

Keep the existing server/plugin identity, GitHub identity-only `read:user` scope,
callback `${ENG_PLATFORM_MCP_PUBLIC_BASE_URL}/api/auth/callback`, public PKCE/DCR,
and existing release process. Do not grant GitHub repository scopes.

Configure the public API origin, private MCP OAuth/audit Firestore collections,
and the existing dedicated BD control collection/project. Durable grants reside
as `source=mcp` session records in that control collection, even for non-BD tools;
there is no production memory fallback. The API service account needs access to
both private stores. The BD execution infrastructure/policy must be active for BD
tools. Existing Cloud Tasks queues and private GCS bucket are reused.

Only `eng-platform.access` is advertised, required and issued. Existing access
and refresh tokens, including previously granted deployment or alert scopes, are
rejected. Users must reconnect through their host's supported OAuth flow and
accept the full consent page. Do not edit registrations, forge credentials or
silently elevate old grants. `/mcp/cost-alerts` remains a compatible URL alias
with the same full-access consent and tools.
Existing public client registrations advertise the current supported scope while
preserving their registered identity, redirects and PKCE metadata. This does
not change stored registrations or grant access: only a fresh GitHub flow and
full browser consent can create the new authorization.

Clients caching legacy scopes are redirected from `/authorize` to a fresh
`eng-platform.access` authorization request only for an existing legacy public
registration. PKCE, registered redirect validation, GitHub login and explicit
full consent still run; token exchange and refresh never accept legacy scopes.
The consent CSP permits only its own form endpoint and the registered callback
origin so Chromium can follow the POST redirect, including loopback SDK clients.

The browser-bound consent names database reads, operational information,
production deploy/rollback and activating the fixed private cost alert. Denial,
wrong browser/CSRF/origin, expiry or replay cannot issue an authorization code.
Access tokens default to one hour; refresh tokens rotate with the existing
30-day validity. A durable grant ID survives rotation, while an atomic credential
generation invalidates the previous access/refresh pair. Concurrent refreshes
cannot produce two active generations. No raw credentials are stored.

## BD tools

| Tool | Contract |
|---|---|
| `list_databases` | Enabled bases and async/sort capabilities |
| `get_database_schema(database_id)` | Tables, full columns/types and service associations |
| `create_database_workspace(database_id)` | Private workspace for this OAuth connection |
| `start_database_query(database_id, workspace_id, sql, client_request_id)` | Async execution ID/status; idempotent |
| `get_database_query(database_id, workspace_id, execution_id)` | Status, typed columns, final total and expiry |
| `get_database_page(..., page_index=0, page_size=25, view_id=None)` | One zero-based page; sizes 25/50/100 |
| `cancel_database_query(...)` | Cancel pending work |
| `create_database_view(..., column_key, direction, client_request_id)` | Async global stable ordering over the capture |
| `get_database_view(..., view_id)` | View status and inherited expiry |
| `cancel_database_view(..., view_id)` | Cancel/purge view, preserving source capture |
| `purge_database_workspace(database_id, workspace_id)` | Invalidate and clean captures/derived objects |

Poll pending status with backoff; do not resubmit SQL or automatically read every
page. Start/create-view return queued/running/completed status DTOs immediately;
MCP tool responses do not use REST HTTP `202` envelopes. Pages preserve typed
unique keys, duplicate headers, exact numeric strings, nulls and complete cells.
No Excel tool is added; existing Web exports continue working.

PostgreSQL executes once with its existing SELECT parser, audit and read-only
transaction/reader. Sorting and pages use the captured data. Retention is one
hour from completion; OAuth rotation never extends it. Workspaces are bound to
one grant/client/user, including isolation between connections of the same user.
Browser logout does not revoke an unrelated MCP connection.

All existing operational limits apply: four minutes, no additional row cap,
256 MiB capture, 1 MiB chunks, 8 MiB pages, 100 columns, 16 KiB/cell and
64 KiB/serialized row; 512 MiB/user and 2 GiB/global reservations; two global
queries, one/user and one/reader; shared global sort/export permit and two
simultaneous result reads. See `database-readonly-console.md` for failure,
cancellation, expiration and cleanup details. Metadata uses its reserved permit.

## Deploy, rollback and alerts

All tools require the same full grant. Deploy/rollback still require `reason`
and `idempotency_key`, remain limited to ten mutations/user/hour, and use exact
`oss-v2` evidence, eligible tags, managed-resource barriers, executor selection,
service locks and idempotency. Clients cannot supply SHA, executor, commands,
credentials or profile overrides. Signed release capabilities attest the MCP
grant; workflow consumption checks its live authority. Web capabilities continue
using the existing operator authorization.

Any full MCP user may activate `send_cost_alert`; its fixed attested private
recipient, server-built content, feature flag, server credential and daily
idempotency remain unchanged. Tests must simulate delivery, not send messages.
BD storage operations use their own admission, not the deployment hourly budget.

## Revocation, verification and incidents

Revoking any retained token from a grant invalidates the entire family, including
later rotated credentials. The same durable record is the atomic BD publication
fence: revoked work cannot publish a completed capture. Revocation invalidates
workspaces and schedules purge; workers observe revocation independently.
The RFC 7009 `/revoke` endpoint authenticates the registered public client and
accepts its retained access/refresh credential without requiring a client secret.
Revocation lookup is separate from active token verification: a credential
rotated during disconnect can revoke its family but cannot execute tools.
Disabling `ENG_PLATFORM_MCP_ENABLED` removes the MCP surface and stops MCP-owned
work without changing normal REST/UI authorization.

Verify full consent and tool discovery in Codex/ChatGPT after reconnection;
check a synthetic paginated query, sorted view and explicit workspace purge.
Verify old tokens fail and Web remains protected. Inspect private sanitized
MCP audits alongside deployment history. Audits contain operation/identity,
safe input fingerprint, IDs and outcome; never SQL, result rows or credentials.
Do not bypass the platform to recover a deployment.
