# Remote MCP for eng-platform

The API exposes a Streamable HTTP MCP server at `/mcp`. It is a second
authenticated client of the existing release command engine, not a new deploy
plane: tag eligibility, exact `oss-v2` evidence, idempotency, active-service
locks, GitHub Actions/Cloud Build selection, candidate validation and rollback
remain owned by the backend.

## Activation

Keep `ENG_PLATFORM_MCP_ENABLED=false` until all of the following are true:

1. Set `ENG_PLATFORM_MCP_PUBLIC_BASE_URL` to the public API origin (for example
   `https://api.example.com`), and optionally `ENG_PLATFORM_MCP_ISSUER_URL` if
   the OAuth issuer is a different public origin.
2. Keep the existing GitHub OAuth callback registered for the API:
   `${ENG_PLATFORM_MCP_PUBLIC_BASE_URL}/api/auth/callback`. MCP reuses it and
   separates its authorization state in private storage, so no second GitHub
   OAuth callback (which GitHub OAuth Apps do not support) is required. The app
   needs only `read:user` for identity. Do not grant repository scopes.
3. Configure `ENG_PLATFORM_ALLOWED_GITHUB_LOGINS`. MCP users must be in this
   same allowlist; an authenticated GitHub identity alone is insufficient.
4. Grant the API service account Firestore access to the private OAuth and
   audit collections. No client secrets, GitHub access tokens or token plaintext
   are stored there.
5. Register a test client through `/.well-known/oauth-authorization-server` and
   use authorization-code PKCE. DCR accepts public clients only:
   `token_endpoint_auth_method: "none"`, HTTPS redirect URIs (or localhost HTTP
   for desktop development), `authorization_code` and `refresh_token` grants.
6. Verify a read-only call, a rejected bad-scope call, and a dry integration
   against an already completed deployment. Only then set the feature flag true.

The server publishes OAuth authorization metadata, DCR, token rotation,
revocation and RFC 9728 protected-resource metadata. Access tokens last one
hour by default; refresh tokens rotate and default to 30 days. Configure those
durations only with `ENG_PLATFORM_MCP_ACCESS_TOKEN_TTL_SECONDS` and
`ENG_PLATFORM_MCP_REFRESH_TOKEN_TTL_SECONDS`.

## Tools and permissions

All tools require `eng-platform.read`. `start_deployment` additionally requires
`eng-platform.deploy`; `start_rollback` requires `eng-platform.rollback`.
Deployment tools require a non-empty `reason` and `idempotency_key`; they are
limited to 10 per allowlisted user each hour by default.

Read tools return only existing public DTOs: catalog/service health, eligible
tags, exact release evidence, releases/deployments, FinOps costs and Cloud Run
metrics. Mutating tools accept only a service, tag or recorded deployment id.
They cannot receive an executor, SHA, shell command, secret or release-profile
override.

Every MCP invocation produces a private audit record with GitHub subject,
OAuth client, tool, a hash of sanitized input, correlated deployment id and
outcome. Prompts, bearer tokens and secret values are not recorded.

## Private cost-alert connection

Keep the ordinary `/mcp` connection and its registration default read-only.
For the approved owner, use a separate opt-in server connection at
`${ENG_PLATFORM_MCP_PUBLIC_BASE_URL}/mcp/cost-alerts`. Its discovery and initial
challenge request only `eng-platform.read` and `eng-platform.cost-alerts.send`,
never deployment or rollback. Let ChatGPT perform its supported public PKCE/DCR
flow; do not create a client, token, or authorization URL by hand.

A previous DCR client registered with only read cannot request send with that
registration. Reusing its refresh token also cannot add permission. The opt-in
server URL gives the client a distinct connection that can register the two
explicitly requested scopes without editing existing registrations or tokens.

GitHub authenticates identity using `read:user`. When send is requested, the
API then shows its own browser-bound consent page to the approved owner. Only
an explicit authorization issues the MCP authorization code; cancellation,
expiry, replay, a different browser or another allowed reader cannot grant it.
The send tool declares its OAuth scopes and returns the documented
`_meta["mcp/www_authenticate"]` challenge so ChatGPT can surface its linking UI.

After promotion, refresh the connection's tool catalog and verify the consent
and granted scopes. This permission alone does not enable delivery:
`ENG_PLATFORM_COST_ALERTS_ENABLED` remains off until the separately approved
server destination and credential configuration are complete. The tool takes
no caller-supplied message or recipient and owns its daily idempotency key.

## Incident handling

Revoking an MCP token immediately invalidates that token. Disable
`ENG_PLATFORM_MCP_ENABLED` to remove the entire surface while keeping normal
REST/UI deployments operating. Inspect the MCP audit collection and the normal
deployment history together; never attempt recovery by dispatching a workflow
or Cloud Build job outside the platform.
