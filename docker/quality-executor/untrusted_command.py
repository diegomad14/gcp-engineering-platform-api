#!/usr/bin/env python3
"""Supervise one repository-owned command from the trusted quality gate."""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import math
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path


_UID = 65532
_GID = 65532
_MAX_COMMAND_BYTES = 128_000
_FORBIDDEN_ENV = {
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_URL",
    "CLOUDSDK_AUTH_ACCESS_TOKEN",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_GHA_CREDS_PATH",
    "GITHUB_TOKEN",
    "GH_TOKEN",
}
_PR_SET_CHILD_SUBREAPER = 36
_TRUSTED_OUTPUT_NAMES = {
    "install.log",
    "tests.log",
    "build.log",
    "lint.log",
    "format.log",
    "typecheck.log",
    "semgrep.log",
    "semgrep.json",
    "trivy.log",
    "trivy.json",
}


def _environment(path: Path) -> dict[str, str]:
    stat = path.stat(follow_symlinks=False)
    if not path.is_file() or stat.st_mode & 0o077:
        raise RuntimeError("Untrusted environment manifest permissions are unsafe")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise RuntimeError("Untrusted environment manifest is invalid")
    if _FORBIDDEN_ENV.intersection(value):
        raise RuntimeError("Credential environment is forbidden in repository commands")
    return value


def _command(encoded: str) -> str:
    try:
        value = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise RuntimeError("Invalid encoded command") from exc
    if not value or len(value) > _MAX_COMMAND_BYTES or b"\x00" in value:
        raise RuntimeError("Invalid repository command")
    return value.decode("utf-8")


def _uid_processes() -> list[int]:
    result: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            status = (entry / "status").read_text(encoding="utf-8")
        except OSError:
            continue
        real_uid = state = ""
        for line in status.splitlines():
            if line.startswith("Uid:"):
                real_uid = line.split()[1]
            elif line.startswith("State:"):
                state = line.split()[1]
        if real_uid == str(_UID) and state != "Z":
            result.append(int(entry.name))
    return result


def _become_subreaper() -> None:
    """Adopt escaped double-forks so the trusted supervisor can reap them."""

    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _reap_children() -> None:
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def _secure_report_exchange(path: Path) -> None:
    """Pin trusted outputs so repo code cannot replace them with symlinks."""

    current = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_uid != 0
        or current.st_gid != 0
        or current.st_mode & stat.S_ISVTX == 0
    ):
        raise RuntimeError("Quality report exchange directory is unsafe")
    for name in _TRUSTED_OUTPUT_NAMES:
        output = path / name
        try:
            metadata = output.stat(follow_symlinks=False)
        except FileNotFoundError:
            metadata = None
        if metadata is not None and (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
        ):
            if stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                output.unlink()
            else:
                raise RuntimeError(f"Trusted quality output {name} is not a file")
            metadata = None
        if metadata is None:
            descriptor = os.open(
                output,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            os.close(descriptor)
        else:
            os.chmod(output, 0o600, follow_symlinks=False)


def _kill_descendants(process_group: int) -> None:
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # A hostile command can call setsid or double-fork out of its original
    # process group. The container reserves this UID exclusively for repo code,
    # so a bounded UID sweep closes that escape before the gate reads outputs.
    for _ in range(10):
        processes = _uid_processes()
        if not processes:
            _reap_children()
            return
        for pid in processes:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(0.02)
        _reap_children()
    if _uid_processes():
        raise RuntimeError("Unable to terminate repository-owned processes")
    _reap_children()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--report-directory", type=Path, required=True)
    parser.add_argument("--deadline", type=float, required=True)
    parser.add_argument("--command", required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("Untrusted command supervisor must start as root")
    if not math.isfinite(args.deadline) or args.deadline <= 0:
        raise RuntimeError("Invalid repository command deadline")
    _become_subreaper()
    environment = _environment(args.environment)
    command = _command(args.command)
    _secure_report_exchange(args.report_directory)
    remaining = args.deadline - time.monotonic()
    if remaining <= 0:
        print("Repository command skipped: quality deadline exceeded", file=sys.stderr)
        return 124
    process = subprocess.Popen(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", command],
        env=environment,
        stdin=subprocess.DEVNULL,
        user=_UID,
        group=_GID,
        extra_groups=[],
        # Reports must remain readable by the trusted root normalizer, which
        # intentionally runs without CAP_DAC_OVERRIDE. No credentials are
        # present in this environment; the checkout is isolated per run.
        umask=0o022,
        start_new_session=True,
    )
    try:
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            returncode = 124
            print("Repository command timed out", file=sys.stderr)
    finally:
        _kill_descendants(process.pid)
        _secure_report_exchange(args.report_directory)
    return returncode if returncode >= 0 else 128 - returncode


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        print(f"untrusted command supervisor: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
