#!/usr/bin/env python3
"""Launch and clean up a disposable Linux x86_64 GitHub Actions runner VM.

The controller intentionally owns no long-lived GitHub or GCP credential. The
GitHub registration token is injected into a temporary NoCloud seed and is not
written to the controller state file.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform
import re
import shutil
import signal

# Commands use explicit argument lists and never invoke a shell.
import subprocess  # nosec B404
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


LABEL_PREFIX = "cgm-release-local-"
LEGACY_LABEL = "cgm-release-local"
NORMAL_LABEL = "ubuntu-latest"
STATE_VERSION = 1
STATE_ROOT = Path(
    os.environ.get(
        "CGM_RELEASE_STATE_DIR",
        str(Path.home() / ".cgm-release-runner" / "state"),
    )
)
ACTIVE_STATUSES = {"queued", "in_progress", "requested", "waiting"}
TERMINAL_CONCLUSIONS = {"success", "failure", "cancelled", "timed_out", "skipped"}
CI_WORKFLOWS = {
    "api ci",
    "web ci",
    "ci",
    "pr check",
    "quality gate",
    "conventional pr title",
}
RELEASE_WORKFLOWS = CI_WORKFLOWS | {
    "semantic release",
    "sonarqube main baseline",
}
DEPLOY_WORKFLOWS = {"platform deploy", "platform rollback"}
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
RUNNER_LABEL_RE = re.compile(r"^cgm-release-local-[0-9a-f]{40}$")
RUNNER_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
RUNNER_NAME_RE = re.compile(r"^cgm-release-local-\d+-\d+$")
APPROVED_ARTIFACTS_PATH = Path(__file__).with_name("approved-artifacts.json")


class ControllerError(RuntimeError):
    """A fail-closed controller error."""


def approved_runner_artifact() -> tuple[str, str]:
    try:
        payload = json.loads(APPROVED_ARTIFACTS_PATH.read_text(encoding="utf-8"))
        runner = payload["github_runner"]
        version = str(runner["version"])
        sha256 = str(runner["sha256"])
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ControllerError("Approved runner artifact manifest is invalid") from exc
    validate_runner_version(version)
    validate_pin(sha256, "approved runner sha256")
    return version, sha256


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def run_command(
    args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    # Callers provide validated executable names and argument lists.
    return subprocess.run(  # nosec B603
        args,
        check=check,
        text=True,
        capture_output=True,
        env={**os.environ, "GH_PAGER": "cat"},
    )


def gh(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run_command(["gh", *args], check=check)


def gh_json(args: list[str]) -> Any:
    result = gh(args)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ControllerError("GitHub returned invalid JSON") from exc


def validate_repo(repo: str) -> None:
    if not REPOSITORY_RE.fullmatch(repo):
        raise ControllerError("--repo must be OWNER/REPOSITORY")


def validate_pin(value: str, name: str) -> None:
    if not SHA256_RE.fullmatch(value):
        raise ControllerError(f"{name} must be a 64-character SHA-256 value")


def validate_runner_version(value: str) -> None:
    if not RUNNER_VERSION_RE.fullmatch(value):
        raise ControllerError("--runner-version must be a semantic version")


def validate_approved_runner(version: str, sha256: str) -> None:
    validate_runner_version(version)
    validate_pin(sha256, "--runner-sha256")
    approved_version, approved_sha256 = approved_runner_artifact()
    if (version, sha256.lower()) != (approved_version, approved_sha256.lower()):
        raise ControllerError(
            "Runner version/hash do not match approved-artifacts.json"
        )


def runner_label_for_sha(sha: str) -> str:
    normalized = sha.lower()
    if not GIT_SHA_RE.fullmatch(normalized):
        raise ControllerError("GitHub run SHA must be a 40-character commit SHA")
    return f"{LABEL_PREFIX}{normalized}"


def is_supported_variable(value: str) -> bool:
    return value in {NORMAL_LABEL, LEGACY_LABEL} or bool(
        RUNNER_LABEL_RE.fullmatch(value)
    )


def variable_value(repo: str) -> str | None:
    result = gh(["variable", "get", "CGM_ACTIONS_RUNNER", "--repo", repo], check=False)
    if result.returncode == 0:
        value = result.stdout.strip()
        if is_supported_variable(value):
            return value
        raise ControllerError(
            "CGM_ACTIONS_RUNNER has an unsupported value; refusing to overwrite it"
        )
    if "not found" in (result.stdout + result.stderr).lower():
        return None
    raise ControllerError("Could not read CGM_ACTIONS_RUNNER")


def set_variable(repo: str, value: str) -> None:
    if value != NORMAL_LABEL and not RUNNER_LABEL_RE.fullmatch(value):
        raise ControllerError("Unsupported CGM_ACTIONS_RUNNER value")
    gh(["variable", "set", "CGM_ACTIONS_RUNNER", "--repo", repo, "--body", value])


def restore_variable(repo: str, previous: str | None, active_label: str) -> None:
    current = variable_value(repo)
    if current == previous:
        return
    if current != active_label:
        raise ControllerError(
            "CGM_ACTIONS_RUNNER no longer matches this incident; refusing to overwrite another operator's value"
        )
    if previous is None:
        gh(
            [
                "variable",
                "delete",
                "CGM_ACTIONS_RUNNER",
                "--repo",
                repo,
                "--yes",
            ]
        )
    else:
        set_variable(repo, previous)


def runner_list(repo: str) -> list[dict[str, Any]]:
    payload = gh_json(["api", f"repos/{repo}/actions/runners?per_page=100"])
    runners = payload.get("runners", [])
    if not isinstance(runners, list):
        raise ControllerError("GitHub returned an invalid runner list")
    return [runner for runner in runners if isinstance(runner, dict)]


def runner_by_name(repo: str, name: str) -> dict[str, Any] | None:
    return next(
        (runner for runner in runner_list(repo) if runner.get("name") == name), None
    )


def active_critical_runs(
    repo: str, target_ids: set[int], target_sha: str
) -> list[dict[str, Any]]:
    runs = gh_json(
        [
            "run",
            "list",
            "--repo",
            repo,
            "--limit",
            "100",
            "--json",
            "databaseId,status,workflowName,headSha,event",
        ]
    )
    conflicts = []
    for run in runs:
        if run.get("databaseId") in target_ids:
            continue
        workflow = str(run.get("workflowName") or "").lower()
        known_workflow = workflow in (
            CI_WORKFLOWS | RELEASE_WORKFLOWS | DEPLOY_WORKFLOWS
        )
        if (
            run.get("status") in ACTIVE_STATUSES
            and known_workflow
            and run.get("headSha") != target_sha
        ):
            conflicts.append(run)
    return conflicts


def related_runs(
    repo: str, target: dict[str, Any], profile: str, expected_sha: str | None
) -> list[dict[str, Any]]:
    if profile == "deploy":
        return [target]
    sha = str(target.get("headSha") or "")
    runs = gh_json(
        [
            "run",
            "list",
            "--repo",
            repo,
            "--commit",
            sha,
            "--limit",
            "100",
            "--json",
            "databaseId,status,conclusion,headSha,headBranch,event,workflowName,url",
        ]
    )
    selected = []
    for run in runs:
        try:
            validate_target_run(repo, run, profile, expected_sha)
        except ControllerError:
            continue
        if not run_succeeded(run):
            selected.append(run)
    if not any(run.get("databaseId") == target.get("databaseId") for run in selected):
        selected.append(target)
    return selected


def run_has_billing_failure(repo: str, run_id: int) -> bool:
    payload = gh_json(["run", "view", str(run_id), "--repo", repo, "--json", "jobs"])
    for job in payload.get("jobs", []):
        job_id = job.get("databaseId")
        if not job_id:
            continue
        annotations = gh_json(["api", f"repos/{repo}/check-runs/{job_id}/annotations"])
        for annotation in annotations:
            message = str(annotation.get("message") or "").lower()
            if (
                "billing" in message
                or "payments have failed" in message
                or "spending limit" in message
            ):
                return True
    return False


def get_run(repo: str, run_id: int) -> dict[str, Any]:
    run = gh_json(
        [
            "run",
            "view",
            str(run_id),
            "--repo",
            repo,
            "--json",
            "databaseId,status,conclusion,headSha,headBranch,event,workflowName,url",
        ]
    )
    if run.get("databaseId") != run_id:
        raise ControllerError("The requested run ID was not returned by GitHub")
    return run


def pull_request_for_sha(repo: str, sha: str) -> dict[str, Any]:
    pulls = gh_json(
        [
            "api",
            f"repos/{repo}/commits/{sha}/pulls",
            "-H",
            "Accept: application/vnd.github+json",
        ]
    )
    matching = [
        pull
        for pull in pulls
        if pull.get("state") == "open" and pull.get("head", {}).get("sha") == sha
    ]
    if len(matching) != 1:
        raise ControllerError(
            "CI fallback requires exactly one open pull request for the SHA"
        )
    return matching[0]


def validate_target_run(
    repo: str, run: dict[str, Any], profile: str, expected_sha: str | None
) -> None:
    sha = str(run.get("headSha") or "").lower()
    runner_label_for_sha(sha)
    if expected_sha and sha != expected_sha.lower():
        raise ControllerError("Target run SHA does not match --expected-sha")
    event = str(run.get("event") or "")
    workflow = str(run.get("workflowName") or "").lower()
    ref = str(run.get("headBranch") or "")
    if profile == "ci":
        if event != "pull_request" or workflow not in CI_WORKFLOWS:
            raise ControllerError(
                "CI fallback accepts only allowlisted pull request workflows"
            )
        pull = pull_request_for_sha(repo, sha)
        head_repo = str(pull.get("head", {}).get("repo", {}).get("full_name") or "")
        base_ref = str(pull.get("base", {}).get("ref") or "")
        if head_repo.lower() != repo.lower() or base_ref != "main":
            raise ControllerError(
                "CI fallback rejects forks and pull requests not targeting main"
            )
        return
    if profile == "release":
        if event != "push" or ref != "main" or workflow not in RELEASE_WORKFLOWS:
            raise ControllerError(
                "Release fallback accepts only allowlisted main push workflows"
            )
        return
    if profile == "deploy":
        if (
            event != "workflow_dispatch"
            or not ref.startswith("v")
            or workflow not in DEPLOY_WORKFLOWS
        ):
            raise ControllerError(
                "Deploy fallback requires an allowlisted version-tag workflow"
            )
        return
    raise ControllerError("Unsupported fallback profile")


def target_run(
    repo: str,
    run_id: int,
    profile: str = "deploy",
    expected_sha: str | None = None,
) -> dict[str, Any]:
    run = get_run(repo, run_id)
    validate_target_run(repo, run, profile, expected_sha)
    return run


def verify_file_sha256(path: Path, expected: str, label: str) -> None:
    validate_pin(expected, label)
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest().lower() != expected.lower():
        raise ControllerError(f"{label} does not match the supplied SHA-256")


def require_prerequisites(image: Path, image_sha256: str) -> None:
    for tool in ("gh", "qemu-system-x86_64", "qemu-img"):
        if not command_exists(tool):
            raise ControllerError(f"Required host tool is missing: {tool}")
    if not any(command_exists(tool) for tool in ("cloud-localds", "hdiutil")):
        raise ControllerError("Required ISO tool is missing: cloud-localds or hdiutil")
    if not image.is_file():
        raise ControllerError(f"VM image does not exist: {image}")
    verify_file_sha256(image, image_sha256, "--image-sha256")
    auth = gh(["auth", "status"], check=False)
    if auth.returncode != 0:
        raise ControllerError("gh is not authenticated")


def state_path(repo: str, run_id: int) -> Path:
    safe_repo = repo.replace("/", "_")
    return STATE_ROOT / f"{safe_repo}-{run_id}.json"


def read_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError(f"Cannot read state file: {path}") from exc
    if state.get("version") != STATE_VERSION:
        raise ControllerError("Unsupported local runner state version")
    validate_repo(str(state.get("repo", "")))
    runner_name = str(state.get("runner_name", ""))
    if not RUNNER_NAME_RE.fullmatch(runner_name):
        raise ControllerError("Invalid temporary runner name in state file")
    if not RUNNER_LABEL_RE.fullmatch(str(state.get("runner_label", ""))):
        raise ControllerError("Invalid temporary runner label in state file")
    return state


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def encode(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def make_user_data(
    *,
    repository: str,
    runner_token: str,
    runner_name: str,
    runner_label: str,
    runner_version: str,
    runner_sha256: str,
    start_docker: bool = True,
) -> str:
    provision_path = Path(__file__).with_name("image-provision.sh")
    provision = provision_path.read_text()
    manifest = Path(__file__).with_name("approved-artifacts.json").read_text()
    template_path = Path(__file__).with_name("guest-bootstrap.sh")
    bootstrap = template_path.read_text()
    encoded_provision = encode(provision)
    encoded_manifest = encode(manifest)
    encoded_bootstrap = encode(bootstrap)
    return f"""#cloud-config
write_files:
  - path: /usr/local/sbin/cgm-release-runner-provision.sh
    permissions: '0700'
    encoding: b64
    content: {encoded_provision}
  - path: /usr/local/sbin/cgm-release-runner-bootstrap.sh
    permissions: '0700'
    encoding: b64
    content: {encoded_bootstrap}
  - path: /tmp/approved-artifacts.json
    permissions: '0444'
    encoding: b64
    content: {encoded_manifest}
runcmd:
  - [ /usr/local/sbin/cgm-release-runner-provision.sh ]
  - [ bash, -c, "CGM_REPOSITORY=$(echo {encode(repository)} | base64 -d) CGM_RUNNER_TOKEN=$(echo {encode(runner_token)} | base64 -d) CGM_RUNNER_NAME=$(echo {encode(runner_name)} | base64 -d) CGM_RUNNER_LABEL=$(echo {encode(runner_label)} | base64 -d) CGM_RUNNER_VERSION=$(echo {encode(runner_version)} | base64 -d) CGM_RUNNER_SHA256=$(echo {encode(runner_sha256)} | base64 -d) CGM_SKIP_DOCKER=$(echo {encode("1" if not start_docker else "0")} | base64 -d) /usr/local/sbin/cgm-release-runner-bootstrap.sh" ]
"""


def qemu_accel() -> list[str]:
    configured = os.environ.get("CGM_RELEASE_QEMU_ACCEL")
    if configured:
        return ["-accel", configured]
    system = platform.system()
    if system == "Linux" and Path("/dev/kvm").exists():
        return ["-accel", "kvm"]
    if system == "Darwin":
        if platform.machine() == "arm64":
            return ["-accel", "tcg,thread=multi"]
        return ["-accel", "hvf"]
    if system == "Windows":
        return ["-accel", "whpx"]
    return ["-accel", "tcg,thread=multi"]


def create_seed_iso(user_data_path: Path, meta_data_path: Path, seed_iso: Path) -> None:
    if command_exists("cloud-localds"):
        result = run_command(
            ["cloud-localds", str(seed_iso), str(user_data_path), str(meta_data_path)],
            check=False,
        )
        if result.returncode != 0:
            raise ControllerError(
                f"Could not create NoCloud seed: {result.stderr.strip()}"
            )
        return
    if command_exists("hdiutil"):
        seed_dir = seed_iso.parent / "seed"
        seed_dir.mkdir(parents=True, exist_ok=True)
        (seed_dir / "user-data").write_text(user_data_path.read_text())
        (seed_dir / "meta-data").write_text(meta_data_path.read_text())
        result = run_command(
            [
                "hdiutil",
                "makehybrid",
                "-iso",
                "-joliet",
                "-default-volume-name",
                "cidata",
                "-o",
                str(seed_iso),
                str(seed_dir),
            ],
            check=False,
        )
        if result.returncode != 0:
            raise ControllerError(
                f"Could not create NoCloud seed: {result.stderr.strip()}"
            )
        return
    raise ControllerError("No supported ISO tool is available")


def create_vm_files(image: Path, user_data: str, work_dir: Path) -> tuple[Path, Path]:
    vm_disk = work_dir / "runner.qcow2"
    seed_iso = work_dir / "seed.iso"
    result = run_command(
        [
            "qemu-img",
            "create",
            "-f",
            "qcow2",
            "-F",
            "qcow2",
            "-b",
            str(image),
            str(vm_disk),
        ],
        check=False,
    )
    if result.returncode != 0:
        raise ControllerError(
            f"Could not create disposable VM disk: {result.stderr.strip()}"
        )
    user_data_path = work_dir / "user-data"
    user_data_path.write_text(user_data)
    meta_data_path = work_dir / "meta-data"
    meta_data_path.write_text("instance-id: cgm-release-local\n")
    create_seed_iso(user_data_path, meta_data_path, seed_iso)
    return vm_disk, seed_iso


def launch_vm(vm_disk: Path, seed_iso: Path) -> subprocess.Popen[str]:
    accel = qemu_accel()
    command = [
        "qemu-system-x86_64",
        *accel,
        "-machine",
        "q35",
        "-m",
        os.environ.get("CGM_RELEASE_VM_MEMORY", "4096"),
        "-smp",
        os.environ.get("CGM_RELEASE_VM_CPUS", "2"),
        "-display",
        "none",
        "-serial",
        "none",
        "-monitor",
        "none",
        "-nic",
        "user,model=virtio",
        "-drive",
        f"if=virtio,format=qcow2,file={vm_disk}",
        "-drive",
        f"format=raw,media=cdrom,readonly=on,file={seed_iso}",
    ]
    # QEMU executable and arguments are assembled from validated values.
    process = subprocess.Popen(  # nosec B603
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    time.sleep(3)
    if process.poll() is not None and accel:
        fallback = [arg for arg in command if arg not in accel]
        process = subprocess.Popen(  # nosec B603
            fallback,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    return process


def terminate_process(pid: int | None) -> None:
    if not pid:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.25)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def delete_runner(repo: str, runner_id: int | None, runner_name: str) -> None:
    if runner_id is None:
        found = runner_by_name(repo, runner_name)
        runner_id = found.get("id") if found else None
    if runner_id is None:
        return
    result = gh(
        [
            "api",
            "--method",
            "DELETE",
            f"repos/{repo}/actions/runners/{runner_id}",
        ],
        check=False,
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr).lower()
        if "404" not in output and "not found" not in output:
            raise ControllerError(
                f"Could not remove temporary runner {runner_id}: {result.stderr.strip()}"
            )


def wait_for_runner_ready(
    repo: str,
    name: str,
    runner_label: str,
    timeout: int,
    state: dict[str, Any],
    path: Path,
) -> None:
    deadline = time.monotonic() + timeout
    last_status = "absent"
    last_missing = {"self-hosted", "linux", "x64", runner_label}
    while time.monotonic() < deadline:
        found = runner_by_name(repo, name)
        if found:
            runner_id = found.get("id")
            if runner_id and state.get("runner_id") != runner_id:
                state["runner_id"] = runner_id
                write_state(path, state)
            last_status = str(found.get("status") or "unknown")
            labels = {
                str(label.get("name"))
                for label in found.get("labels", [])
                if isinstance(label, dict)
            }
            last_missing = {"self-hosted", "linux", "x64", runner_label} - labels
            if last_status == "online" and not last_missing:
                return
        time.sleep(5)
    raise ControllerError(
        "Temporary runner did not become ready before timeout: "
        f"status={last_status}, missing_labels={sorted(last_missing)}"
    )


def wait_for_run(repo: str, run_id: int, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = get_run(repo, run_id)
        if (
            run.get("status") == "completed"
            and run.get("conclusion") in TERMINAL_CONCLUSIONS
        ):
            return run
        time.sleep(10)
    raise ControllerError(
        "Target GitHub run did not reach a terminal state before timeout"
    )


def rerun_same_run(repo: str, run_id: int, run: dict[str, Any], timeout: int) -> None:
    status = run.get("status")
    if status == "completed":
        gh(["run", "rerun", str(run_id), "--repo", repo])
        return
    if status == "in_progress":
        raise ControllerError(
            "The target run is already in progress; refusing to cancel an active release"
        )
    if status not in {"queued", "requested", "waiting"}:
        raise ControllerError(
            f"The target run is not recoverable from status {status!r}"
        )

    # GitHub cannot move an already-queued job to a different `runs-on` label.
    # Canceling and rerunning the same run ID makes the temporary variable take
    # effect without creating a new workflow run or changing its SHA/tag.
    gh(["run", "cancel", str(run_id), "--repo", repo])
    cancelled = wait_for_run(repo, run_id, timeout)
    if cancelled.get("conclusion") == "success":
        return
    gh(["run", "rerun", str(run_id), "--repo", repo])


def activate_runs(
    *,
    repo: str,
    runs: list[dict[str, Any]],
    timeout: int,
    selection_mode: str,
    runner_label: str,
    state: dict[str, Any],
    path: Path,
) -> None:
    if selection_mode == "explicit":
        return
    state["variable_change_started"] = True
    write_state(path, state)
    set_variable(repo, runner_label)
    state["variable_changed"] = True
    write_state(path, state)
    for run in runs:
        rerun_same_run(repo, int(run["databaseId"]), run, timeout)


def wait_for_runs(
    repo: str, runs: list[dict[str, Any]], timeout: int
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    pending = {int(run["databaseId"]) for run in runs}
    final: dict[int, dict[str, Any]] = {}
    while pending and time.monotonic() < deadline:
        for run_id in list(pending):
            current = get_run(repo, run_id)
            if (
                current.get("status") == "completed"
                and current.get("conclusion") in TERMINAL_CONCLUSIONS
            ):
                final[run_id] = current
                pending.remove(run_id)
        if pending:
            time.sleep(10)
    if pending:
        raise ControllerError(
            f"GitHub runs did not reach a terminal state before timeout: {sorted(pending)}"
        )
    return [final[int(run["databaseId"])] for run in runs]


def run_succeeded(run: dict[str, Any]) -> bool:
    return run.get("status") == "completed" and run.get("conclusion") == "success"


def cleanup(state_path_value: Path, state: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if state.get("variable_change_started") or state.get("variable_changed"):
        try:
            restore_variable(
                state["repo"],
                state.get("previous_runner_variable"),
                state["runner_label"],
            )
        except ControllerError as exc:
            errors.append(str(exc))
    try:
        delete_runner(state["repo"], state.get("runner_id"), state["runner_name"])
    except Exception as exc:  # pragma: no cover - provider failure path
        errors.append(f"runner removal failed: {exc}")
    terminate_process(state.get("vm_pid"))
    try:
        vm_dir = Path(state["vm_dir"]).expanduser().resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if vm_dir.parent != temp_root or not vm_dir.name.startswith(
            "cgm-release-runner-"
        ):
            raise ControllerError(
                "Refusing to delete a VM directory outside the controller temp root"
            )
        if vm_dir.exists():
            shutil.rmtree(vm_dir)
    except (KeyError, OSError, ControllerError) as exc:
        errors.append(f"VM cleanup failed: {exc}")
    if not errors:
        state_path_value.unlink(missing_ok=True)
    return errors


def run_up(args: argparse.Namespace) -> int:
    validate_repo(args.repo)
    if args.run_id <= 0:
        raise ControllerError("--run-id must be positive")
    if not args.expected_sha:
        raise ControllerError("--expected-sha is required")
    if args.selection_mode == "explicit" and args.profile != "deploy":
        raise ControllerError("Explicit runner selection is allowed only for deploy")
    validate_approved_runner(args.runner_version, args.runner_sha256)
    image = Path(args.image).expanduser().resolve()
    require_prerequisites(image, args.image_sha256)
    run = target_run(args.repo, args.run_id, args.profile, args.expected_sha)
    if run.get("status") == "completed" and run.get("conclusion") == "success":
        raise ControllerError(
            "The requested run already succeeded; no recovery is needed"
        )
    sha = str(run["headSha"]).lower()
    runner_label = runner_label_for_sha(sha)
    runs = related_runs(args.repo, run, args.profile, args.expected_sha)
    if args.profile in {"ci", "release"}:
        for candidate in runs:
            if (
                candidate.get("status") == "completed"
                and candidate.get("conclusion") == "failure"
                and not run_has_billing_failure(args.repo, int(candidate["databaseId"]))
            ):
                raise ControllerError(
                    f"Run {candidate['databaseId']} failed for a reason other than GitHub Billing"
                )
    run_ids = {int(candidate["databaseId"]) for candidate in runs}
    conflicts = active_critical_runs(args.repo, run_ids, sha)
    if conflicts:
        names = ", ".join(str(item.get("workflowName")) for item in conflicts)
        raise ControllerError(
            f"Active critical workflows found; acquire incident lock first: {names}"
        )

    path = (
        Path(args.state_file).expanduser()
        if args.state_file
        else state_path(args.repo, args.run_id)
    )
    if path.exists():
        raise ControllerError(f"A local runner incident is already active: {path}")
    previous = variable_value(args.repo)
    if previous not in {None, NORMAL_LABEL}:
        raise ControllerError("CGM_ACTIONS_RUNNER is already in contingency mode")
    runner_name = f"cgm-release-local-{args.run_id}-{int(time.time())}"
    work_dir = Path(tempfile.mkdtemp(prefix="cgm-release-runner-"))
    state = {
        "version": STATE_VERSION,
        "repo": args.repo,
        "run_id": args.run_id,
        "run_url": run.get("url"),
        "sha": run.get("headSha"),
        "workflow": run.get("workflowName"),
        "profile": args.profile,
        "run_ids": sorted(run_ids),
        "selection_mode": args.selection_mode,
        "runner_name": runner_name,
        "runner_label": runner_label,
        "runner_id": None,
        "previous_runner_variable": previous,
        "variable_change_started": False,
        "variable_changed": False,
        "vm_pid": None,
        "vm_dir": str(work_dir),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_state(path, state)
    try:
        registration = gh_json(
            [
                "api",
                "--method",
                "POST",
                f"repos/{args.repo}/actions/runners/registration-token",
            ]
        )
        token = registration.get("token")
        if not token:
            raise ControllerError("GitHub did not return a runner registration token")
        user_data = make_user_data(
            repository=args.repo,
            runner_token=token,
            runner_name=runner_name,
            runner_label=runner_label,
            runner_version=args.runner_version,
            runner_sha256=args.runner_sha256,
        )
        vm_disk, seed_iso = create_vm_files(image, user_data, work_dir)
        process = launch_vm(vm_disk, seed_iso)
        state["vm_pid"] = process.pid
        write_state(path, state)
        wait_for_runner_ready(
            args.repo,
            runner_name,
            runner_label,
            args.runner_timeout,
            state,
            path,
        )
        # cloud-init has completed registration once the runner is online; the
        # NoCloud seed is no longer needed and contains the registration token.
        for secret_path in (work_dir / "seed.iso", work_dir / "user-data"):
            secret_path.unlink(missing_ok=True)
        activate_runs(
            repo=args.repo,
            runs=runs,
            timeout=args.run_timeout,
            selection_mode=args.selection_mode,
            runner_label=runner_label,
            state=state,
            path=path,
        )
        print(f"Local runner online for {args.repo}; state: {path}")
        final_runs = wait_for_runs(args.repo, runs, args.run_timeout)
        for final in final_runs:
            print(
                f"Run {final.get('databaseId')} finished: {final.get('conclusion')} "
                f"SHA={final.get('headSha')} URL={final.get('url')}"
            )
        errors = cleanup(path, state)
        if errors:
            for error in errors:
                print(f"CLEANUP ERROR: {error}", file=sys.stderr)
            return 2
        return 0 if all(run_succeeded(final) for final in final_runs) else 2
    except KeyboardInterrupt:
        print("Interrupted; cleaning up the local runner", file=sys.stderr)
        errors = cleanup(path, state)
        for error in errors:
            print(f"CLEANUP ERROR: {error}", file=sys.stderr)
        return 130 if not errors else 2
    except Exception:
        cleanup_errors = cleanup(path, state)
        for error in cleanup_errors:
            print(f"CLEANUP ERROR: {error}", file=sys.stderr)
        raise


def run_validate(args: argparse.Namespace) -> int:
    validate_repo(args.repo)
    validate_approved_runner(args.runner_version, args.runner_sha256)
    image = Path(args.image).expanduser().resolve()
    require_prerequisites(image, args.image_sha256)

    runner_name = f"cgm-release-local-{int(time.time())}-{os.getpid()}"
    runner_label = LABEL_PREFIX + hashlib.sha256(runner_name.encode()).hexdigest()[:40]
    work_dir = Path(tempfile.mkdtemp(prefix="cgm-release-runner-"))
    path = (
        Path(args.state_file).expanduser().resolve()
        if args.state_file
        else STATE_ROOT / f"{args.repo.replace('/', '_')}-validate-{runner_name}.json"
    )
    state = {
        "version": STATE_VERSION,
        "repo": args.repo,
        "runner_name": runner_name,
        "runner_label": runner_label,
        "runner_id": None,
        "previous_runner_variable": None,
        "variable_change_started": False,
        "variable_changed": False,
        "vm_pid": None,
        "vm_dir": str(work_dir),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    write_state(path, state)
    try:
        registration = gh_json(
            [
                "api",
                "--method",
                "POST",
                f"repos/{args.repo}/actions/runners/registration-token",
            ]
        )
        token = registration.get("token")
        if not token:
            raise ControllerError("GitHub did not return a runner registration token")
        user_data = make_user_data(
            repository=args.repo,
            runner_token=token,
            runner_name=runner_name,
            runner_label=runner_label,
            runner_version=args.runner_version,
            runner_sha256=args.runner_sha256,
            start_docker=args.profile == "release",
        )
        vm_disk, seed_iso = create_vm_files(image, user_data, work_dir)
        process = launch_vm(vm_disk, seed_iso)
        state["vm_pid"] = process.pid
        write_state(path, state)
        wait_for_runner_ready(
            args.repo,
            runner_name,
            runner_label,
            args.runner_timeout,
            state,
            path,
        )
        for secret_path in (work_dir / "seed.iso", work_dir / "user-data"):
            secret_path.unlink(missing_ok=True)
        print(f"Local runner validated for {args.repo}; state: {path}")
        errors = cleanup(path, state)
        if errors:
            for error in errors:
                print(f"CLEANUP ERROR: {error}", file=sys.stderr)
            return 2
        return 0
    except KeyboardInterrupt:
        print("Interrupted; cleaning up the local runner", file=sys.stderr)
        errors = cleanup(path, state)
        for error in errors:
            print(f"CLEANUP ERROR: {error}", file=sys.stderr)
        return 130 if not errors else 2
    except Exception:
        cleanup_errors = cleanup(path, state)
        for error in cleanup_errors:
            print(f"CLEANUP ERROR: {error}", file=sys.stderr)
        raise


def run_down(args: argparse.Namespace) -> int:
    path = Path(args.state_file).expanduser().resolve()
    state = read_state(path)
    errors = cleanup(path, state)
    for error in errors:
        print(f"CLEANUP ERROR: {error}", file=sys.stderr)
    return 2 if errors else 0


def parser() -> argparse.ArgumentParser:
    approved_version, approved_sha256 = approved_runner_artifact()
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    up = subparsers.add_parser("up", help="launch, rerun and clean up one blocked run")
    up.add_argument("--repo", required=True)
    up.add_argument("--run-id", required=True, type=int)
    up.add_argument("--expected-sha", required=True)
    up.add_argument("--image", required=True)
    up.add_argument("--image-sha256", required=True)
    up.add_argument("--runner-version", default=approved_version)
    up.add_argument("--runner-sha256", default=approved_sha256)
    up.add_argument("--runner-timeout", type=int, default=300)
    up.add_argument("--run-timeout", type=int, default=7200)
    up.add_argument("--profile", choices=("ci", "release", "deploy"), default="deploy")
    up.add_argument(
        "--selection-mode",
        choices=("variable", "explicit"),
        default="variable",
        help="use the repository variable fallback or an explicit workflow input",
    )
    up.add_argument("--state-file")
    validate = subparsers.add_parser(
        "validate", help="register a temporary runner, verify labels, then clean up"
    )
    validate.add_argument("--repo", required=True)
    validate.add_argument("--image", required=True)
    validate.add_argument("--image-sha256", required=True)
    validate.add_argument("--runner-version", default=approved_version)
    validate.add_argument("--runner-sha256", default=approved_sha256)
    validate.add_argument("--runner-timeout", type=int, default=300)
    validate.add_argument(
        "--profile", choices=("registration", "release"), default="release"
    )
    validate.add_argument("--state-file")
    down = subparsers.add_parser("down", help="recover an interrupted incident")
    down.add_argument("--state-file", required=True)
    return parser


if __name__ == "__main__":

    def _interrupt_on_termination(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _interrupt_on_termination)
    arguments = parser().parse_args()
    try:
        if arguments.command == "up":
            exit_code = run_up(arguments)
        elif arguments.command == "validate":
            exit_code = run_validate(arguments)
        else:
            exit_code = run_down(arguments)
    except ControllerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 2
    except subprocess.CalledProcessError as exc:
        print(
            f"ERROR: command failed: {' '.join(exc.cmd)}\n{exc.stderr.strip()}",
            file=sys.stderr,
        )
        exit_code = 2
    raise SystemExit(exit_code)
