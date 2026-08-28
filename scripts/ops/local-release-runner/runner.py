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


LABEL = "cgm-release-local"
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
CRITICAL_WORKFLOW_MARKERS = (
    "semantic release",
    "platform deploy",
    "platform rollback",
    "release candidate",
    "promote",
    "rollback",
)
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
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


def variable_value(repo: str) -> str | None:
    result = gh(["variable", "get", "CGM_ACTIONS_RUNNER", "--repo", repo], check=False)
    if result.returncode == 0:
        value = result.stdout.strip()
        if value in {NORMAL_LABEL, LABEL}:
            return value
        raise ControllerError(
            "CGM_ACTIONS_RUNNER has an unsupported value; refusing to overwrite it"
        )
    if "not found" in (result.stdout + result.stderr).lower():
        return None
    raise ControllerError("Could not read CGM_ACTIONS_RUNNER")


def set_variable(repo: str, value: str) -> None:
    if value not in {NORMAL_LABEL, LABEL}:
        raise ControllerError("Unsupported CGM_ACTIONS_RUNNER value")
    gh(["variable", "set", "CGM_ACTIONS_RUNNER", "--repo", repo, "--body", value])


def restore_variable(repo: str, previous: str | None) -> None:
    current = variable_value(repo)
    if current == previous:
        return
    if current != LABEL:
        raise ControllerError(
            "CGM_ACTIONS_RUNNER is no longer cgm-release-local; refusing to overwrite another operator's value"
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


def active_critical_runs(repo: str, target_id: int) -> list[dict[str, Any]]:
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
        if run.get("databaseId") == target_id:
            continue
        workflow = str(run.get("workflowName") or "").lower()
        if run.get("status") in ACTIVE_STATUSES and any(
            marker in workflow for marker in CRITICAL_WORKFLOW_MARKERS
        ):
            conflicts.append(run)
    return conflicts


def target_run(repo: str, run_id: int) -> dict[str, Any]:
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
    if run.get("event") == "pull_request":
        raise ControllerError("Pull request workflows cannot use the local runner")
    if run.get("event") not in {"push", "workflow_dispatch"}:
        raise ControllerError("Only trusted push/workflow_dispatch runs are allowed")
    workflow = str(run.get("workflowName") or "").lower()
    if not any(marker in workflow for marker in CRITICAL_WORKFLOW_MARKERS):
        raise ControllerError(
            "Only release/deploy/rollback workflows can use the local runner"
        )
    ref = str(run.get("headBranch") or "")
    if ref != "main" and not ref.startswith("v"):
        raise ControllerError("The local runner requires main or a version tag")
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
    for tool in ("gh", "qemu-system-x86_64", "qemu-img", "cloud-localds"):
        if not command_exists(tool):
            raise ControllerError(f"Required host tool is missing: {tool}")
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
    runner_version: str,
    runner_sha256: str,
) -> str:
    template_path = Path(__file__).with_name("guest-bootstrap.sh")
    bootstrap = template_path.read_text()
    encoded_bootstrap = encode(bootstrap)
    return f"""#cloud-config
write_files:
  - path: /usr/local/sbin/cgm-release-runner-bootstrap.sh
    permissions: '0700'
    encoding: b64
    content: {encoded_bootstrap}
runcmd:
  - [ bash, -c, "CGM_REPOSITORY=$(echo {encode(repository)} | base64 -d) CGM_RUNNER_TOKEN=$(echo {encode(runner_token)} | base64 -d) CGM_RUNNER_NAME=$(echo {encode(runner_name)} | base64 -d) CGM_RUNNER_VERSION=$(echo {encode(runner_version)} | base64 -d) CGM_RUNNER_SHA256=$(echo {encode(runner_sha256)} | base64 -d) /usr/local/sbin/cgm-release-runner-bootstrap.sh" ]
"""


def qemu_accel() -> list[str]:
    configured = os.environ.get("CGM_RELEASE_QEMU_ACCEL")
    if configured:
        return ["-accel", configured]
    system = platform.system()
    if system == "Linux" and Path("/dev/kvm").exists():
        return ["-accel", "kvm"]
    if system == "Darwin":
        return ["-accel", "hvf"]
    if system == "Windows":
        return ["-accel", "whpx"]
    return ["-accel", "tcg,thread=multi"]


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
    result = run_command(
        ["cloud-localds", str(seed_iso), str(user_data_path)], check=False
    )
    if result.returncode != 0:
        raise ControllerError(f"Could not create NoCloud seed: {result.stderr.strip()}")
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


def wait_for_runner(
    repo: str, name: str, timeout: int, state: dict[str, Any], path: Path
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = runner_by_name(repo, name)
        if found and found.get("status") == "online":
            state["runner_id"] = found.get("id")
            write_state(path, state)
            return
        time.sleep(5)
    raise ControllerError("Temporary runner did not become online before timeout")


def wait_for_run(repo: str, run_id: int, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = target_run(repo, run_id)
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


def cleanup(state_path_value: Path, state: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if state.get("variable_change_started") or state.get("variable_changed"):
        try:
            restore_variable(state["repo"], state.get("previous_runner_variable"))
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
    validate_runner_version(args.runner_version)
    validate_pin(args.runner_sha256, "--runner-sha256")
    approved_version, approved_sha256 = approved_runner_artifact()
    if (args.runner_version, args.runner_sha256.lower()) != (
        approved_version,
        approved_sha256.lower(),
    ):
        raise ControllerError(
            "Runner version/hash do not match approved-artifacts.json"
        )
    image = Path(args.image).expanduser().resolve()
    require_prerequisites(image, args.image_sha256)
    run = target_run(args.repo, args.run_id)
    if run.get("status") == "completed" and run.get("conclusion") == "success":
        raise ControllerError(
            "The requested run already succeeded; no recovery is needed"
        )
    conflicts = active_critical_runs(args.repo, args.run_id)
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
        "runner_name": runner_name,
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
            runner_version=args.runner_version,
            runner_sha256=args.runner_sha256,
        )
        vm_disk, seed_iso = create_vm_files(image, user_data, work_dir)
        process = launch_vm(vm_disk, seed_iso)
        state["vm_pid"] = process.pid
        write_state(path, state)
        wait_for_runner(args.repo, runner_name, args.runner_timeout, state, path)
        # cloud-init has completed registration once the runner is online; the
        # NoCloud seed is no longer needed and contains the registration token.
        for secret_path in (work_dir / "seed.iso", work_dir / "user-data"):
            secret_path.unlink(missing_ok=True)
        # Persist intent before the remote mutation so `down` can recover even
        # if the developer process is interrupted between these two actions.
        state["variable_change_started"] = True
        write_state(path, state)
        set_variable(args.repo, LABEL)
        state["variable_changed"] = True
        write_state(path, state)
        rerun_same_run(args.repo, args.run_id, run, args.run_timeout)
        print(f"Local runner online for {args.repo}; state: {path}")
        final = wait_for_run(args.repo, args.run_id, args.run_timeout)
        print(
            f"Run {args.run_id} finished: {final.get('conclusion')} "
            f"SHA={final.get('headSha')} URL={final.get('url')}"
        )
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
    up.add_argument("--image", required=True)
    up.add_argument("--image-sha256", required=True)
    up.add_argument("--runner-version", default=approved_version)
    up.add_argument("--runner-sha256", default=approved_sha256)
    up.add_argument("--runner-timeout", type=int, default=300)
    up.add_argument("--run-timeout", type=int, default=7200)
    up.add_argument("--state-file")
    down = subparsers.add_parser("down", help="recover an interrupted incident")
    down.add_argument("--state-file", required=True)
    return parser


if __name__ == "__main__":
    arguments = parser().parse_args()
    try:
        exit_code = (
            run_up(arguments) if arguments.command == "up" else run_down(arguments)
        )
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
