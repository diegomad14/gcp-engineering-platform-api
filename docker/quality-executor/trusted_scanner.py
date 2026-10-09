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

from untrusted_command import _kill_descendants, _uid_processes


_UID = 65532
_GID = 65532
_SCANNER_RUNTIME_PARENT_ENV = "ENG_PLATFORM_SCANNER_RUNTIME_PARENT"
_SCANNER_RUNTIME_PARENT_NAME = "trusted-scanner-runtime"
_MAX_OUTPUT_BYTES = 128 * 1024 * 1024
_SEMGREP_ERROR_ENTRY_LIMIT = 4096
_SEMGREP_ERROR_COUNT_LIMIT = 65535
_SCANNER_DETAIL_LIMIT = 500
_SEMGREP_ERROR_GENERIC = "Semgrep reported scan errors"
# Exact JSON labels from Semgrep 1.136.0's semgrep-interfaces revision
# 85c728ef38c1aef822f28035078fa2671ec7d10a, semgrep_output_v1.atd.
# Diagnostic categories only: never use this mapping to accept/reject a scan.
_SEMGREP_ERROR_CATEGORIES = {
    "Lexical error": "parse",
    "Syntax error": "parse",
    "Other syntax error": "parse",
    "AST builder error": "parse",
    "PartialParsing": "partial_parse",
    "Timeout": "timeout",
    "Fixpoint timeout": "timeout",
    "Timeout during interfile analysis": "timeout",
    "Out of memory": "memory",
    "OOM during interfile analysis": "memory",
    "Stack overflow": "memory",
    "Rule parse error": "rule",
    "InvalidRuleSchemaError": "rule",
    "UnknownLanguageError": "rule",
    "Invalid YAML": "rule",
    "PatternParseError": "rule",
    "Pattern parse error": "rule",
    "IncompatibleRule": "rule",
    "Incompatible rule": "rule",
    "Missing plugin": "rule",
    "Internal matching error": "internal",
    "SemgrepError": "internal",
    "Fatal error": "internal",
}
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


def _validate_scanner_directory_chain(
    directory: Path, *, scanner_parent: bool = False
) -> None:
    value = str(directory)
    if (
        not directory.is_absolute()
        or value.startswith("//")
        or os.path.normpath(value) != value
    ):
        raise TrustedScannerError("Scanner parent must be canonical absolute")
    for current in (*reversed(directory.parents), directory):
        try:
            metadata = current.stat(follow_symlinks=False)
        except OSError as exc:
            raise TrustedScannerError("Scanner parent is unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise TrustedScannerError("Scanner parent contains a non-directory")
        if (metadata.st_uid, metadata.st_gid) != (0, 0):
            raise TrustedScannerError("Scanner parent chain must be root-owned")
        if not metadata.st_mode & 0o001:
            raise TrustedScannerError("Scanner parent chain is not traversable")
        writable = bool(metadata.st_mode & 0o022)
        if writable and not metadata.st_mode & stat.S_ISVTX:
            raise TrustedScannerError("Scanner parent chain is replaceable")
        if scanner_parent and current == directory:
            if writable or stat.S_IMODE(metadata.st_mode) != 0o711:
                raise TrustedScannerError("Scanner parent must have mode 0711")


def _scanner_runtime_parent() -> Path:
    value = os.environ.get(_SCANNER_RUNTIME_PARENT_ENV, "")
    if not value or str(Path(value)) != value:
        raise TrustedScannerError("Scanner parent must be canonical absolute")
    parent = Path(value)
    if parent.name != _SCANNER_RUNTIME_PARENT_NAME:
        raise TrustedScannerError("Scanner parent name is not authorized")
    _validate_scanner_directory_chain(parent, scanner_parent=True)
    return parent


def _runtime_environment() -> dict[str, str]:
    parent = _scanner_runtime_parent()
    # Repo commands and scanners share UID 65532. Require the previous
    # supervisor's cleanup to have completed before starting a scanner phase.
    if _uid_processes():
        raise TrustedScannerError("Untrusted processes remain before scanner phase")
    runtime = Path(tempfile.mkdtemp(prefix="eng-platform-scanner-", dir=str(parent)))
    # Configure permissions while root still owns the directory: the executor
    # deliberately lacks CAP_FOWNER once ownership is transferred.
    os.chmod(runtime, 0o700)
    os.chown(runtime, _UID, _GID, follow_symlinks=False)
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(runtime),
        "TMPDIR": str(runtime),
        "XDG_CACHE_HOME": str(runtime / ".cache"),
        "TRIVY_CACHE_DIR": str(runtime / ".cache" / "trivy"),
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


def _semgrep_error_category(error: object) -> str:
    if not isinstance(error, dict):
        return "malformed"
    kind = error.get("type")
    if isinstance(kind, list):
        kind = kind[0] if kind else None
    if not isinstance(kind, str):
        return "malformed"
    # Do not hash or inspect arbitrarily long, attacker-controlled labels.
    if len(kind) > 64:
        return "other"
    return _SEMGREP_ERROR_CATEGORIES.get(kind, "other")


def _semgrep_error_summary(errors: object) -> str:
    if not isinstance(errors, list):
        return f"{_SEMGREP_ERROR_GENERIC}: state=INVALID_OUTPUT"
    total = len(errors)
    processed = min(total, _SEMGREP_ERROR_ENTRY_LIMIT)
    counts = dict.fromkeys(
        (
            "parse",
            "partial_parse",
            "timeout",
            "memory",
            "rule",
            "internal",
            "other",
            "malformed",
        ),
        0,
    )
    for index in range(processed):
        counts[_semgrep_error_category(errors[index])] += 1
    fields = " ".join(f"{category}={count}" for category, count in counts.items())
    return (
        f"{_SEMGREP_ERROR_GENERIC}: state=COLLECTED "
        f"total={min(total, _SEMGREP_ERROR_COUNT_LIMIT)} "
        f"capped={int(total > _SEMGREP_ERROR_COUNT_LIMIT)} "
        f"processed={processed} incomplete={int(total > processed)} "
        f"entry_limit={_SEMGREP_ERROR_ENTRY_LIMIT} "
        f"count_limit={_SEMGREP_ERROR_COUNT_LIMIT} {fields}"
    )


def _semgrep_error_detail(errors: object) -> str:
    # A diagnostic failure must never replace the existing rejection, reveal
    # its exception/payload, or overflow the normalized detail's 500 characters.
    try:
        summary = _semgrep_error_summary(errors)
        if not isinstance(summary, str):
            return _SEMGREP_ERROR_GENERIC
        line = f"trusted scanner: {summary}"
        if (
            not line.isascii()
            or not line.isprintable()
            or len(line) > _SCANNER_DETAIL_LIMIT
        ):
            return _SEMGREP_ERROR_GENERIC
        return summary
    except Exception:
        return _SEMGREP_ERROR_GENERIC


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
        raise TrustedScannerError(_semgrep_error_detail(errors))
    # Semgrep reports parser limitations as PartialParsing even when the rest
    # of the file was scanned. Preserve these in the sealed report, but fail
    # closed on any scanner/runtime/configuration error.
    for error in errors:
        kind = error.get("type") if isinstance(error, dict) else None
        if not isinstance(kind, list) or not kind or kind[0] != "PartialParsing":
            raise TrustedScannerError(_semgrep_error_detail(errors))


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
