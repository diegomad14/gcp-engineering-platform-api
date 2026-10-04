# Runtime catalog fixtures and private activation

## Public examples are not production inventory

The checked-in example has **51 resources: 22 Services and 29 Jobs**, comprising
**19 previously public managed examples** and **32 synthetic observation-only
examples (7 Services and 25 Jobs)**. Every public example has
`logs: {enabled: false, allowed_logins: []}`. It contains no operational log-reader
allowlist and is not an inventory or activation record for a production deployment.

The synthetic entries are `demo-observed-service-01` through
`demo-observed-service-07` and `demo-observed-job-01` through
`demo-observed-job-25`, in the fictitious project `demo-platform-prod` and region
`us-central1`. Their timestamp `2020-01-01T00:00:00+00:00` and
`cloud_run_inventory` provenance are simulated schema fixtures, not a claim of
provider observation. Repository and owner are null; names never imply ownership,
readiness, existence or permission.

| Example group | Services | Jobs | Log policy |
|---|---:|---:|---|
| Previously public managed examples | 15 | 4 | Disabled, no readers |
| Synthetic observation-only examples | 7 | 25 | Disabled, no readers |
| Total | 22 | 29 | Disabled, no readers |

## Private authority and approval

Operational resource coordinates, inventory evidence and reader policies belong
in a separately reviewed private runtime catalog, outside the repository and
public build inputs. Production log access requires the configured private file
and its SHA-256 integrity pin; a missing, unreadable, corrupt or mismatched
private authority fails closed without falling back to the examples.

The global feature defaults to OFF and its reader allowlist defaults to empty.
Access requires real GitHub OAuth, membership in the independent global reader
list and an explicit enabled resource policy. Approval covers only the reviewed
identities and readers. New resources remain disabled with empty reader lists.
The shared maximum remains 12 upstream Logging queries per minute across all
resources, source projects, replicas and revisions. A catalog policy grants no
IAM or deployment capability and proves no provider connectivity.

## Review and rollout

1. Review the complete private catalog and its provenance without publishing
   inventory names, timestamps or reader lists. Keep unknown ownership explicit.
2. Generate Service or Job proposals with explicit identity, project, region and
   runtime kind. Service Factory and the local catalog-registration CLI generate
   disabled policies only and cannot replace or promote existing identities.
3. Review readers separately from registration. Missing policies deny access;
   malformed or duplicate authority fails closed. Deployment rights, ownership,
   repository membership and IAP headers do not grant log access.
4. Verify existing Logging and Firestore access, one shared quota project and
   replica/revision consistency. Follow the drain procedure in the
   [runtime logs runbook](runtime-logs.md). Do not broaden IAM or reset budgets as
   a side effect of registration or source configuration.
5. Run the exact-commit `oss-v2` gate from `AGENTS.md` before canonical release.
   Offline tests use a fully synthetic, temporary private catalog of 51 records,
   with only `demo-reader` enabled. They cover resource and global ACLs, global
   OFF without upstream calls, future Service/Job registrations remaining OFF,
   management barriers and 500-resource growth without production inventory.
6. After explicit operational approval and prerequisites, activate only the
   reviewed private policy and global reader list. Verify access and revocation
   without adding test identities to the production allowlist. An empty bounded
   sample does not prove the resource never ran or has no other log entries.

## Observation-only management barrier

Observation-only identities retain null repository/owner, unknown environment
and cost center, disabled deployment/quality and empty secret/validation bindings.
Server-side barriers reject deploy, rollback, release, secret mutations and
Factory adoption before providers or stores. Detail reads never fabricate
readiness. A separately approved log policy does not grant managed capabilities.

The public catalog exposes `logs.enabled` and `logs.configured`, never readers.
`configured` means a valid explicit policy exists, not that IAM or connectivity
has been verified. Full identity and policy are revalidated around I/O; metadata
and log caches cannot preserve access after authority changes.
