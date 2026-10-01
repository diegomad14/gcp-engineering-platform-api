# Artemis API: verified serving web commit

`ENG_PLATFORM_ARTEMIS_WEB_SHA` in the Engineering Platform API is the expected
web commit passed to the Artemis API release executor. The executor compares it
with the commit labels of every web revision serving traffic and blocks an API
deployment on a mismatch. Keep this validation enabled.

After publishing Artemis Web, verify its serving revision and exact release
evidence, then stage the corresponding full SHA in Engineering Platform with
`--no-traffic`. Publish that configuration through a new eligible maintenance
release of Engineering Platform before requesting the next Artemis API deploy.
Preserve all other environment values, secret references, identity and traffic.

## Verified configuration on 2026-10-01

- Serving web revision: `cgm-artemis-web-ep-281cd3adc6` (100% traffic).
- Web repository: `diegomad14/cgm-artemis-web`.
- Expected web commit: `823f67594ec7198f822ebfcbed23b6c9fe4cd5ba`.
- Superseded configuration: `73ce9f339d30992f950ba657b897e63a8fb401a4`.
- Maintenance source starts from the serving Engineering Platform release
  `v0.31.3` / `ab93703984ac31b09336e2c3eb50ee0e703c5ac7`; no functional source
  change is needed for this configuration correction.

The blocked Artemis attempt remains in deployment history. Retrying Artemis uses
its original eligible tag and a new idempotency key after configuration is
published. This procedure does not redeploy Artemis Web or any Artemis worker.
