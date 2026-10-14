# Private runtime probes with GitHub WIF

MCP deployment `7078883882`, GitHub run `38648429092`, failed in
`deploy_runtime -> runtime_ready(candidate=True)` before a runtime HTTP request:
`gcloud auth print-identity-token --audiences=...` rejected the external-account
credential type. This is not evidence of a `run.invoker` denial. The audience
already came from the base service's `status.url`; a candidate tag URL or
`/ready` path must not replace it.

The shared `runtime_identity_token` helper serves both private readiness and
smoke probes. It reads the existing auth action's external-account configuration
in memory, obtains a fresh GitHub subject token, exchanges it at STS and uses
the **federated STS token** directly on IAMCredentials `generateIdToken` for the
same service account named by `service_account_impersonation_url`. The body uses
the canonical service audience and `includeEmail=true`. There is no intermediate
service-account access token, self-impersonation, delegate chain or added IAM.

Token/configuration failures stop the probe. They cannot fall back to another
identity, reach the runtime, grant activation or change traffic. Token requests
are bounded, refuse redirects and emit only static stage/status diagnostics.
Tokens and headers stay in memory. Public probes do not read credentials or
mint tokens. Non-WIF CLI behavior is retained.

Google documents this STS-to-ID-token flow and requires the service URL as the
audience even for a traffic-tag request:
https://docs.cloud.google.com/run/docs/authenticating/service-to-service

The IAM method requires `iam.serviceAccounts.getOpenIdToken`; invoking the
private runtime is a separate authorization check. Neither live permission was
proved by the original local CLI failure. Do not infer a missing grant, add a
role, or change the audience to hide a failure. Stop and report the exact
principal/resource/permission if a real request is denied.

## Publication and consumption

1. Require repository CI and exact `oss-v2` evidence for the reviewed fix.
2. Publish **only** `release-executor` with the existing
   `publish-release-tooling.yml` GitHub workflow and tooling WIF. The publisher
   checks the image's engine SHA256 against reviewed source before pushing and
   records source commit, code SHA256 and immutable image digest. No Cloud Build.
3. Coordinate `ENG_PLATFORM_RELEASE_EXECUTOR_IMAGE` and staged code hashes in
   `gcp-engineering-platform-api`, `cgm-artemis-api` and `cgm-artemis-web`.
   Deploy and rollback callers consume the image variable; candidate/promote
   callers additionally check the engine code hash. Publish corrected caller
   workflows through their normal gates and new immutable releases as needed.
   An existing tag keeps its original workflow hash; changing main cannot change
   it. Worker `phase=full` did not execute the staged hash guard in the failed run.
4. Verify the published digest contains the reviewed engine bytes, and inspect
   actual caller pins/variables. Do not claim effective consumption while a
   required variable update remains inaccessible. Repository Variables read/write
   access is separate from permission to edit workflow source.
5. Hand the verified release/digest to the deployment owner. This publication
   task does not deploy MCP, roll back production, delete the inactive candidate,
   change IAM/credentials, or activate the general 7-release/15-deploy defaults.

The completed runner's ephemeral ADC file is not present in the cloud development
container. Protocol tests use in-memory synthetic configuration and transports;
they do not mint real credentials or prove live invocation permissions.
