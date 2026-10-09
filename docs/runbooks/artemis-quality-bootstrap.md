# Artemis PR 172 quality bootstrap

This control consumes one server-owned, durable ticket for quality validation of
diegomad14/cgm-artemis-api (repository ID 1306114845), service cgm-artemis-api,
PR 172. The approved head and base are fixed in release_quality_bootstrap.py;
callers supply only an execution ID and an idempotency key. The old failed
execution remains unchanged.

The approved publication head is 73b8f336eafd0827c9fdcbcc01a9ba53cf2aeda9,
confirmed by the parent after creating the private GitHub commit object without
moving any ref or starting CI. Its parent is the original PR head
89973710ff9e8fb021369b643ba03f7640ef72e6. The local review commit
4e2be62cbf954a2fc90ad4adb912da711d2023b9 has the same complete tree,
88c53b750722f4b7e347e8f5c393ccdb407e48bb, and all four changed blobs match.
The preserved Git bundle describes that local review object; it does not
replace the approved remote commit identity. PR 172 remains on its original
head until the parent coordinates publication. Authentication and push have
not been attempted by this cloud preparation task.

The API requires the existing authenticated, allowlisted deployer. The registered
MCP tool request_quality_bootstrap uses eng-platform.access, live credential and
grant validation, the existing mutation rate limit and sanitized audit.
Authorization is checked before reservation and immediately before submission.
The public response contains execution status, provider/build/run IDs and a
validated Cloud Console logs link.

## Reservation and submission

A transaction consumes the fixed global ticket
artemis-pr172-quality-bootstrap-v1 and changes a fresh waiting_github execution
directly to submitting, with one attempt consumed. Production requires
persistent storage; unavailable persistence cannot fall back to memory.
The ticket binds actor, hashed key, repository/PR, head/base, canonical
fingerprint, profile, executor digest, policy, complete build request and a
private attempt nonce. An ephemeral dispatch permit is stored only as a hash
and can be claimed once by the winning process.

The request uses the canonical server profile, exact timeout 3600s and
CLOUD_LOGGING_ONLY. machineType, pool, workerPool and diskSizeGb are absent.
The maximum authorized compute charge is USD 0.36; auxiliary charges remain
separate. The known defective Python executor digest
sha256:1e5121ba9ebeb640d5da512e92f44507184d911cd0b81e2daf97e3519cbc682e
is rejected before consuming the ticket. Configure the repaired image's actual
registry digest through the canonical rollout before operating this control.

There is no retry: HTTP 401 cannot replay the POST, transport adapters have no
retries and redirects are disabled. Revocation, drift, process failure,
timeouts, malformed responses and failed outcome persistence leave the attempt
consumed. Reusing the same key returns public status. Scheduler claims,
absence reconciliation, generic resets and late GitHub callbacks cannot reopen
the attempt or overwrite its provider.

Recovery is read-only. It paginates candidate builds and binds exactly one build
whose source/connection/revision, account, complete steps/images, substitutions,
profile, nonce, timeout and options match the reserved request. Ambiguous or
altered candidates fail closed. Unbound source/event callbacks use the same
unique recovery before token issuance. Canonical reports, immutable evidence,
provider verification and truthful PASS/FAIL checks remain required.

## Planned operational sequence

Preparing this patch performs none of these remote actions.

1. Publish the repaired Python tooling image and deploy the central control
   through the canonical process; verify the actual digest and MCP tools list.
2. Record the ID and active state of eng-platform-quality.yml, disable it
   through the official GitHub control, and verify disabled_manually with no
   active canonical runs.
3. Publish the exact approved PR head. Its webhook reserves a new execution
   waiting for GitHub. Revalidate current head/base, disabled workflow identity
   and absence of any canonical run for that revision. For pull_request_target,
   use workflow ID/path, event, eng-platform-quality-{head} title and PR metadata
   across all pages; a run's head_sha alone cannot establish absence.
4. Invoke the control once. Follow its public state and canonical evidence.
   Restore the workflow to active after PASS, FAIL or abort, and before merge.
   On uncertainty, restore it operationally without resetting or resubmitting
   the consumed ticket.

Deployment WIF variables, IAM, secrets, executor circuits, existing deployment
exceptions and automatic billing-only fallback remain unchanged.

## Python runtime isolation

The private workflows reserve /tmp as rw,noexec,nosuid,nodev,size=64m and a
separate scratch tmpfs as rw,exec,nosuid,nodev,size=6080m. These two agreed
allocations total 6144 MiB. Existing Docker IPC and /dev/shm are preserved.

The motor's --scratch /eng-platform-scratch/quality contract places checkout and
venv under scratch, outside the output volume. It derives the sibling scanner
runtime parent, validates a root-owned traversable chain without symlinks and a
protected parent of mode 0711, then creates each scanner's private 0700 runtime.
It sets the mode before transferring ownership to UID/GID 65532, without
adding CAP_FOWNER. HOME, TMPDIR, XDG cache and Trivy cache remain inside scratch,
with no /tmp fallback.

Repository commands and scanners share UID/GID 65532. Mode 0700 does not
separate those actors: phases remain serial, descendant cleanup is preserved
and scanner startup rejects any remaining process of that UID.

The official Python image publisher runs a real Docker smoke before push under
read-only root, dropped capabilities plus existing CHOWN/SETUID/SETGID/KILL,
no-new-privileges and the exact two tmpfs mounts. The smoke uses a pinned,
hash-verified native wheel offline, checks native loading as UID 65532, the
fresh nested scratch path, scanner runtime construction, a synthetic cache
larger than 64 MiB, the process-phase barrier and protected paths. It runs no
scanners and sends no metrics. Smoke failure prevents image publication.
