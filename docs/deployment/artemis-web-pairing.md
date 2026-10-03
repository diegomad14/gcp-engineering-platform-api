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

- Serving web revision: `cgm-artemis-web-ep-fca79057fc` (100% traffic).
- Web repository: `diegomad14/cgm-artemis-web`.
- Expected web commit: `16a6b9aed69c03b09ca27b2991008fc8b2e5db36` (`v1.40.0`).
- Superseded configuration: `d6bd3d3bc4f2a9bf937b80fbaba9019582a1ef21`.
- Maintenance source starts from the serving Engineering Platform release
  `v0.32.4` / `66fb598c0e2adcccdc748e8c6f4fb320db6880ad`; no functional source
  change is needed for this configuration correction.

The blocked Artemis attempt `6822385097` remains in deployment history. Its API
retry `6823043470` succeeded as `v1.48.0` before Web `v1.40.0` was published.
Publish this second Engineering Platform maintenance release before the Jobs
phase. This procedure does not redeploy Artemis Web or any Artemis worker.

For later Web releases, repeat this procedure using only the full commit actually
serving traffic, confirmed by its release evidence. Never advance the pin to a
candidate or merely eligible web release.
