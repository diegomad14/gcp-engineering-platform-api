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

- Serving web revision: `cgm-artemis-web-ep-efa8364cb4` (100% traffic).
- Web repository: `diegomad14/cgm-artemis-web`.
- Expected web commit: `2f37ce20be43133e888b6dac4ee18f902652a38a` (`v1.40.1`).
- Superseded configuration: `16a6b9aed69c03b09ca27b2991008fc8b2e5db36`.
- Maintenance source starts from the serving Engineering Platform release
  `v0.32.5` / `f4c79c69355710a26de477add8a272860254a3fb`; no functional source
  change is needed for this configuration correction.

The Jobs release is published, including Web `v1.40.1`. This maintenance aligns
the central pin with the verified serving web commit after that release.
Jobs recovery remains disabled. This procedure does not redeploy Artemis API,
Web or any Artemis worker, or enable recovery.

For later Web releases, repeat this procedure using only the full commit actually
serving traffic, confirmed by its release evidence. Never advance the pin to a
candidate or merely eligible web release.
