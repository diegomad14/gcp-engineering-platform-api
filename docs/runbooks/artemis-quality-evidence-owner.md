# Artemis derived runtime quality evidence

The quality execution belongs to `cgm-artemis-api`. The eleven reviewed derived
runtimes consume that report for the exact same repository and source SHA.
The SHA-pinned `verify-quality` action contains the explicit runtime-to-owner
registry. Caller environment, execution configuration, repository aliases and
files in the application checkout cannot extend it. New runtimes require a
reviewed registry change and a new action pin.

The API's `for_release=true` endpoint still recalculates the catalog policy.
The action independently requires the exact owner, repository and SHA, passed
`oss-v2`, current evidence and passed checks. Deploy callers also supply
`release-tag`, which must resolve through `refs/tags/<tag>^{commit}` to that SHA.
Pre-tag quality/release callers leave this input empty. The signed deployment
authorization, candidate identity, private smoke and promotion gates remain in
their existing workflows.

## Distributing the correction

Failed MCP deployment `7077678684`, Actions run `38640436734`, used Artemis
`v1.51.14` at `159851bcb9cfdc81d5e6b0b1612bcc14f461d253`. Its workflow pins
`verify-quality` to `ac9d68554c8b6332013785a5bcf451eabfee93ec`. That action
compares the report owner directly to the runtime name and rejects the API
report before build or candidate creation.

1. Merge the platform correction only after CI and exact `oss-v2` evidence pass.
2. In an Artemis application PR, pin every `verify-quality` call to that reviewed
   platform commit and pass `release-tag: ${{ inputs.tag }}` at deploy gates.
3. Pass Artemis PR and main quality/release gates and publish the next release
   using the canonical GitHub release workflow. Verify the new immutable tag
   contains the corrected action pin and has its own exact `oss-v2` report.
4. Give that tag and SHA to the deployment owner for a **new** MCP deployment.
   The workflow executes from the requested tag; updating main or rerunning the
   failed `v1.51.14` job does not update its immutable action pin.

Do not move `v1.51.14`, rewrite stored quality ownership, change dispatch to an
unapproved branch, bypass gates, or deploy MCP as part of distributing this fix.
No platform runtime/catalog activation, new IAM or credentials are needed for
this action-only correction. The general GitHub defaults catalog rollout is a
separate operation.
