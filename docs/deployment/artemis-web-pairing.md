# Artemis API: verified serving web commit

`ENG_PLATFORM_ARTEMIS_WEB_SHA` in the Engineering Platform API is the expected
web commit passed to the Artemis API release executor. The executor compares it
with the commit labels of every web revision serving traffic and blocks an API
deployment on a mismatch. Keep this validation enabled.

Before deploying Artemis Web, retain its exact release evidence in the
access-controlled deployment record. After publishing Web, verify its serving
revision against that evidence, then stage the corresponding full SHA in
Engineering Platform with
`--no-traffic`. Publish that configuration through a new eligible maintenance
release of Engineering Platform before requesting the next Artemis API deploy.
Preserve all other environment values, secret references, identity and traffic.

After the maintenance deployment succeeds, read `ENG_PLATFORM_ARTEMIS_WEB_SHA`
from the Engineering Platform revision actually serving traffic and compare it
with every serving Web revision. A staged `--no-traffic` revision does not update
the pin used by the previous serving revision. Verify this pin explicitly even
when the generic candidate configuration checks pass.

## Recording a maintenance target

Record the serving Web revision, verified full commit and release tag, previous
pin and rollback target in an access-controlled deployment record. Keep private
application SHAs and runtime identifiers out of this public runbook.

Use the verified record when staging the pin and starting the canonical
maintenance release. Retain exact-SHA oss-v2 evidence and candidate/production
checks, then record the new serving Engineering Platform revision and the
verified pin after promotion.

For later Web releases, repeat this procedure using only the full commit actually
serving traffic, confirmed by its release evidence. Never advance the pin to a
candidate or merely eligible web release.

## Live-tag eligibility

The currently live tag is excluded from eligible deployments and release-evidence
lookups. Retain exact-SHA evidence before promotion; a post-promotion eligibility
error does not invalidate the evidence already verified. Do not use another
service name to bypass this check. Updating the serving pin requires a new,
eligible Engineering Platform maintenance release through the canonical flow.
