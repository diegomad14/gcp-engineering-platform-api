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

## Verified maintenance target on 2026-10-05

- Serving web revision: `cgm-artemis-web-ep-6c0528f4cb` (100% traffic).
- Web repository: `diegomad14/cgm-artemis-web`.
- Expected web commit: `232b889589c7d6c56bb78246cb6edb1ac7b497b3` (`v1.41.0`).
- Superseded configuration: `2f37ce20be43133e888b6dac4ee18f902652a38a`.
- Maintenance source starts from the serving Engineering Platform release
  `v0.37.5` / `ad4df25fc6c7567f31f1210a806e689b109f47d0`; no functional source
  change is needed for this configuration correction.

The notification update is published in Web `v1.41.0`. This maintenance aligns
the central pin with that verified serving commit without changing the pairing
validation. This procedure does not redeploy Artemis API, Web or any Artemis
worker, change permissions, or alter recovery settings. Retry the original
eligible Artemis API tag only after the maintenance release and live pin are
verified.

For later Web releases, repeat this procedure using only the full commit actually
serving traffic, confirmed by its release evidence. Never advance the pin to a
candidate or merely eligible web release.
