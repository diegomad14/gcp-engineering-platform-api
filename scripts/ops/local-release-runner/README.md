# Local emergency release runner

This tool is a break-glass control plane for GitHub Actions. The developer's
Windows, macOS or Linux machine only launches QEMU and controls GitHub. The
workflow itself runs in a disposable Linux x86_64 VM.

The tool deliberately fails closed unless all of these conditions are met:

- the run was accepted by GitHub and is blocked by runner availability/capacity;
- the run is a trusted `push` or `workflow_dispatch`, never a pull request;
- the VM image and the Actions Runner archive have explicit SHA-256 pins;
- QEMU, `qemu-img` and `cloud-localds` are available;
- the repository has no other active release/deploy workflow;
- the target repository is registered with the temporary label
  `cgm-release-local`.

Billing, spending-limit and account-suspension errors must be resolved in
GitHub first. A local runner is not a billing bypass.

## Host prerequisites

Install the following tools on the host (or expose them through WSL2 on
Windows):

- Python 3.11+;
- authenticated `gh` with repository Actions administration permissions;
- `qemu-system-x86_64`, `qemu-img`, and either `cloud-localds` (Linux) or
  `hdiutil` (macOS) to create the NoCloud seed;
- an approved image built on a trusted Linux builder with `virt-customize`
  (`libguestfs-tools`). Docker Engine, GitHub CLI, Google Cloud CLI, Node/npm,
  Python, curl, jq and the pinned GitHub Runner are installed before an
  incident occurs.

The controller runs on macOS, Linux and Windows/WSL2. The VM is still **x86_64**,
so on Apple Silicon it runs by emulation. The approved image must still be built
on a Linux x86_64 builder with `libguestfs-tools` (see above); `cloud-localds` is
only required on Linux.

The image must be supplied explicitly and verified with its SHA-256 checksum;
the runner archive is independently pinned and verified too. It must not
contain repository credentials, GCP keys, SSH keys or personal files. The
guest gets outbound NAT only; no host directory or Docker socket is mounted.

`approved-artifacts.json` is the reviewed source of Ubuntu, GitHub Runner and
the released runner image versions/checksums. The manual **Build local runner
image** workflow is available only from `main`. It builds on `ubuntu-24.04`,
publishes the compressed QCOW2 to
`ghcr.io/diegomad14/cgm-release-runner-image`, and reports both its OCI digest
and QCOW2 SHA-256. A follow-up review pins those values in the manifest.

After that review, download the approved image by immutable digest. The command
verifies both the OCI digest (through ORAS) and the QCOW2 SHA-256 before moving
the file into place:

```bash
./scripts/ops/local-release-runner/fetch-image.sh \
  /approved/images/cgm-release-local-ubuntu-24.04-amd64.qcow2
```

To reproduce the build locally on a trusted Linux x86_64 builder, run:

```bash
./scripts/ops/local-release-runner/build-image.sh \
  /approved/images/cgm-release-local-ubuntu-24.04-amd64.qcow2
```

## Start a recovery run

Run the controller from the Engineering Platform API checkout. The `--repo`
argument identifies the repository containing the blocked workflow:

```bash
./scripts/ops/local-release-runner/start.sh up \
  --repo diegomad14/cgm-bot-core \
  --run-id 32447686865 \
  --expected-sha '<40_CHARACTER_COMMIT_SHA>' \
  --profile ci \
  --image /approved/images/cgm-release-local-ubuntu-24.04-amd64.qcow2 \
  --image-sha256 '<IMAGE_SHA256>'
```

Use `ci` for allowlisted checks on an internal pull request, `release` for
allowlisted `main` push workflows, and `deploy` for a tagged deploy or rollback.
CI rejects forks and PRs whose base is not `main`. The controller derives the
only accepted custom label as `cgm-release-local-<SHA>` and recovers all
allowlisted failing runs for that same SHA. Completed CI/release runs are
eligible only when their job annotations prove a GitHub Billing failure.

Engineering Platform dispatches contingency deploys with the SHA-bound label.
For those already-queued runs, add `--selection-mode explicit`; the controller
preserves the same run without changing `CGM_ACTIONS_RUNNER`. CI and release
use the default `variable` mode and re-run the existing failed run IDs.

The runner version/hash are loaded from `approved-artifacts.json`. Overrides
are accepted only when they match that manifest exactly.

The controller records the previous value of `CGM_ACTIONS_RUNNER`, registers
the runner with a short-lived token, waits for it to become online, sets the
temporary repository variable and reruns the existing run when GitHub permits
it. It never creates a tag or a parallel workflow.

PowerShell hosts use the equivalent wrapper:

```powershell
.\scripts\ops\local-release-runner\start.ps1 up `
  --repo diegomad14/cgm-bot-core `
  --run-id 32447686865 `
  --expected-sha '<40_CHARACTER_COMMIT_SHA>' `
  --profile ci `
  --image C:\approved\images\cgm-release-local-ubuntu-24.04-amd64.qcow2 `
  --image-sha256 '<IMAGE_SHA256>'
```

Pressing Ctrl-C runs the same cleanup path as normal completion. If the host
crashes, use the state file printed by the tool:

```bash
./scripts/ops/local-release-runner/start.sh down --state-file '<STATE_FILE>'
```

Cleanup restores the exact previous repository variable, removes the
repository runner by ID and destroys the temporary VM disk. If the variable
was changed by another operator, cleanup refuses to overwrite it and leaves a
clear reconciliation error.

## Validate the runner before an incident

`validate` runs the same disposable-VM lifecycle as `up` but never touches
`CGM_ACTIONS_RUNNER` and never reruns a workflow. It boots the VM, registers a
repo-scoped runner, waits for it to come online, verifies the labels
`self-hosted`, `linux`, `x64` and a disposable SHA-shaped label, then deregisters the
runner and destroys the VM. The default `release` profile also starts Docker
and proves `docker info`; use `registration` only for the shorter registration
smoke test:

```bash
./scripts/ops/local-release-runner/start.sh validate \
  --repo diegomad14/<REPO> \
  --image /approved/images/cgm-release-local-ubuntu-24.04-amd64.qcow2 \
  --image-sha256 '<IMAGE_SHA256>' \
  --profile release
```

Use `validate` when the contingency is not yet active to prove the image and
controller are ready, without mutating the repository's runner variable.

The protected **Local runner production E2E** workflow accepts only an
allowlisted target repository, exact SHA, profile, approved image digest and
existing `run_id`. Store the temporary
fine-grained administrator token only in its `scrum54-validation` environment
as `CGM_RUNNER_ADMIN_TOKEN`. Revoke the token and delete the environment secret
immediately after the supervised validation; the guest receives only GitHub's
short-lived runner registration token.

## Security contract

- Direct host runners are prohibited.
- The runner is repository-scoped and uses only `cgm-release-local-<SHA>`.
- Only internal PRs targeting `main` can use the label; forks are rejected.
- Workflows continue to authenticate to GCP through WIF/OIDC.
- No JSON service-account keys, PATs or host credentials enter the guest.
- The Actions registration token exists only in the temporary NoCloud seed and
  guest memory; it is never written to repository state or controller state.
- Destroying the VM is mandatory because runner deregistration alone cannot
  undo compromise of a host.

See [[release_process]] and [[Runbook - GitHub Actions bloqueado]] for the
incident decision tree and evidence requirements.
