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

## Verified maintenance target on 2026-10-03

- Serving web revision: `cgm-artemis-web-ep-39f5fed3a6` (100% traffic).
- Web repository: `diegomad14/cgm-artemis-web`.
- Expected web commit: `d6bd3d3bc4f2a9bf937b80fbaba9019582a1ef21` (`v1.39.1`).
- Superseded configuration: `823f67594ec7198f822ebfcbed23b6c9fe4cd5ba`.
- Maintenance source starts from the serving Engineering Platform release
  `v0.32.3` / `b6da80de53967b6114e3da649ac9e03840b465d8`; no functional source
  change is needed for this configuration correction.

The blocked Artemis attempt `6822385097` remains in deployment history. Retrying
Artemis uses its original eligible tag and a new idempotency key after
configuration is published. This procedure does not redeploy Artemis Web or any
Artemis worker.

After publishing Artemis Web `v1.40.0`, repeat this maintenance procedure using
only the full commit actually serving traffic, confirmed by its release evidence.
Complete the second Engineering Platform maintenance release before the Jobs
phase. Never advance the pin to a candidate or merely eligible web release.
