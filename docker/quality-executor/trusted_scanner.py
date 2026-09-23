#!/usr/bin/env python3
"""Run pinned scanners as the untrusted UID and seal their JSON as root."""

from __future__ import annotations

import json
import math
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

from untrusted_command import _kill_descendants


_UID = 65532
_GID = 65532
_MAX_OUTPUT_BYTES = 128 * 1024 * 1024
_BINARIES = {
    "semgrep": Path("/usr/local/bin/semgrep"),
    "trivy": Path("/usr/local/bin/trivy"),
}
_OUTPUT_NAMES = {
    "semgrep": "semgrep.json",
    "trivy": "trivy.json",
}


class TrustedScannerError(RuntimeError):
    """The trusted scanner contract was violated."""


def _trusted_binary(scanner: str) -> Path:
    try:
        binary = _BINARIES[scanner]
    except KeyError as exc:
        raise TrustedScannerError("Unsupported trusted scanner") from exc
    if not binary.is_absolute():
        raise TrustedScannerError("Trusted scanner path must be absolute")
    try:
        metadata = binary.stat(follow_symlinks=False)
    except OSError as exc:
        raise TrustedScannerError("Pinned scanner binary is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_mode & 0o022
        or metadata.st_nlink != 1
        or not metadata.st_mode & 0o111
    ):
        raise TrustedScannerError("Pinned scanner binary permissions are unsafe")
    return binary


def _deadline() -> float:
    try:
        value = float(os.environ["ENG_PLATFORM_SCANNER_DEADLINE"])
    except (KeyError, ValueError) as exc:
        raise TrustedScannerError("Trusted scanner deadline is missing") from exc
    if not math.isfinite(value) or value <= 0:
        raise TrustedScannerError("Trusted scanner deadline is invalid")
    return value


def _runtime_environment() -> dict[str, str]:
    # The gate's TMPDIR is root-only. Scanner processes drop to UID 65532,
    # so their private runtime must have a traversable parent.
    runtime = Path(tempfile.mkdtemp(prefix="eng-platform-scanner-", dir="/tmp"))
    os.chown(runtime, _UID, _GID, follow_symlinks=False)
    os.chmod(runtime, 0o700)
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(runtime),
        "TMPDIR": str(runtime),
        "XDG_CACHE_HOME": str(runtime / ".cache"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


def _run_process(binary: Path, args: list[str], stdout: BinaryIO | None) -> int:
    remaining = _deadline() - time.monotonic()
    if remaining <= 0:
        return 124
    process = subprocess.Popen(
        [str(binary), *args],
        env=_runtime_environment(),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        user=_UID,
        group=_GID,
        extra_groups=[],
        umask=0o022,
        start_new_session=True,
    )
    try:
        try:
            return process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            print("Trusted scanner timed out", file=sys.stderr)
            return 124
    finally:
        _kill_descendants(process.pid)


def _output_argument(scanner: str, args: list[str]) -> tuple[list[str], Path]:
    cleaned: list[str] = []
    outputs: list[str] = []
    index = 0
    while index < len(args):
        argument = args[index]
        if argument == "--output":
            if index + 1 >= len(args):
                raise TrustedScannerError("Scanner output argument is incomplete")
            outputs.append(args[index + 1])
            index += 2
            continue
        if argument.startswith("--output="):
            outputs.append(argument.partition("=")[2])
            index += 1
            continue
        cleaned.append(argument)
        index += 1
    if len(outputs) != 1:
        raise TrustedScannerError("Scanner must declare exactly one JSON output")
    target = Path(outputs[0])
    report_directory = Path(os.environ.get("ENG_PLATFORM_TRUSTED_REPORT_DIRECTORY", ""))
    if (
        not target.is_absolute()
        or target.name != _OUTPUT_NAMES[scanner]
        or target.parent.resolve() != report_directory.resolve()
    ):
        raise TrustedScannerError("Scanner output path is not authorized")
    return cleaned, target


def _validate_target(path: Path) -> None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise TrustedScannerError("Trusted scanner target is unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_mode & 0o077
        or metadata.st_nlink != 1
    ):
        raise TrustedScannerError("Trusted scanner target is unsafe")


def _staging_directory() -> Path:
    directory = Path(os.environ.get("ENG_PLATFORM_SCANNER_STAGING_DIRECTORY", ""))
    try:
        metadata = directory.stat(follow_symlinks=False)
    except OSError as exc:
        raise TrustedScannerError("Scanner staging directory is unavailable") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_mode & 0o077
    ):
        raise TrustedScannerError("Scanner staging directory is unsafe")
    return directory


def _read_normalized_capture(path: Path, handle: BinaryIO) -> dict[str, object]:
    metadata = path.stat(follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or metadata.st_mode & 0o077
        or metadata.st_nlink != 1
        or metadata.st_size > _MAX_OUTPUT_BYTES
    ):
        raise TrustedScannerError("Scanner capture was replaced or is too large")
    handle.flush()
    os.fsync(handle.fileno())
    handle.seek(0)
    try:
        value = json.loads(
            handle.read().decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise TrustedScannerError("Scanner did not produce valid JSON") from exc
    if not isinstance(value, dict):
        raise TrustedScannerError("Scanner JSON must be an object")
    return value


def _validate_scanner_result(scanner: str, value: dict[str, object]) -> None:
    if scanner != "semgrep":
        return
    paths = value.get("paths")
    if (
        not isinstance(paths, dict)
        or not isinstance(paths.get("scanned"), list)
        or not paths["scanned"]
    ):
        raise TrustedScannerError("Semgrep did not scan any source files")
    errors = value.get("errors", [])
    if not isinstance(errors, list):
        raise TrustedScannerError("Semgrep reported scan errors")
    # Semgrep reports parser limitations as PartialParsing even when the rest
    # of the file was scanned. Preserve these in the sealed report, but fail
    # closed on any scanner/runtime/configuration error.
    for error in errors:
        kind = error.get("type") if isinstance(error, dict) else None
        if not isinstance(kind, list) or not kind or kind[0] != "PartialParsing":
            raise TrustedScannerError("Semgrep reported scan errors")


def _seal_output(target: Path, value: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                value,
                handle,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        metadata = temporary.stat(follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or metadata.st_nlink != 1
        ):
            raise TrustedScannerError("Normalized scanner output is unsafe")
        temporary.replace(target)
        _validate_target(target)
    finally:
        temporary.unlink(missing_ok=True)


def run(scanner: str, args: list[str]) -> int:
    if os.geteuid() != 0:
        raise TrustedScannerError("Trusted scanner wrapper must run as root")
    binary = _trusted_binary(scanner)
    if args == ["--version"] or args == ["version"]:
        return _run_process(binary, args, None)
    cleaned, target = _output_argument(scanner, args)
    cleaned = _trusted_scan_args(scanner, cleaned)
    _validate_target(target)
    staging = _staging_directory()
    descriptor, capture_name = tempfile.mkstemp(
        prefix=f".{scanner}-capture.", suffix=".json", dir=staging
    )
    capture_path = Path(capture_name)
    try:
        os.chmod(capture_path, 0o600)
        with os.fdopen(descriptor, "w+b", closefd=True) as capture:
            returncode = _run_process(binary, cleaned, capture)
            value = _read_normalized_capture(capture_path, capture)
            _validate_scanner_result(scanner, value)
        _seal_output(target, value)
        return returncode
    finally:
        capture_path.unlink(missing_ok=True)


def _trusted_scan_args(scanner: str, args: list[str]) -> list[str]:
    if scanner != "trivy":
        return args
    if any(item == "--ignorefile" or item.startswith("--ignorefile=") for item in args):
        raise TrustedScannerError("Scanner ignore policy is server-owned")
    return [*args, "--ignorefile", "/opt/eng-platform/trivyignore.yaml"]


def main() -> int:
    if len(sys.argv) < 2:
        raise TrustedScannerError("Trusted scanner name is required")
    return run(sys.argv[1], sys.argv[2:])


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, TrustedScannerError) as exc:
        print(f"trusted scanner: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
