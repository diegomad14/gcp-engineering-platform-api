"""Sanitize untrusted local observations before copying them to the result volume."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

from quality_evidence import (
    EvidenceError,
    MAX_BYTES,
    MAX_FILES,
    source_path,
    unavailable,
    validate_observation,
)


def read_json_file(path: Path, limit: int) -> Any:
    """Bound reads before parsing and reject non-regular/symlink/hardlinked files."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > limit
        ):
            raise EvidenceError("Unsafe or oversized observation file")
        data = handle.read(limit + 1)
        if len(data) > limit:
            raise EvidenceError("Observation file exceeds byte limit")
    try:
        return json.loads(data)
    except (ValueError, RecursionError) as exc:
        raise EvidenceError("Invalid observation JSON") from exc


def export_observation(
    *,
    root: Path,
    report_directory: Path,
    tracked_paths: set[str],
    source_tree: str,
    config_sha256: str,
    fixture_sha256: str,
    command: str,
    runtime: str,
    working_directory: Path | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    if runtime != "python":
        return unavailable("unsupported_runtime")
    try:
        coverage = read_json_file(report_directory / "coverage.json", 16 * 1024 * 1024)
    except FileNotFoundError:
        return unavailable("missing_coverage")
    except (OSError, EvidenceError):
        return unavailable("invalid_coverage")
    try:
        manifest = read_json_file(report_directory / "test-manifest.json", MAX_BYTES)
    except FileNotFoundError:
        return unavailable("missing_test_manifest")
    except (OSError, EvidenceError):
        return unavailable("invalid_test_manifest")
    try:
        if not isinstance(coverage, dict) or not isinstance(
            coverage.get("files"), dict
        ):
            raise EvidenceError("Invalid coverage files")
        if not 1 <= len(coverage["files"]) <= MAX_FILES:
            raise EvidenceError("Invalid coverage file count")
        if (
            not isinstance(manifest, dict)
            or set(manifest)
            != {"schema_version", "tests", "dependencies", "runtime", "measurement_ms"}
            or type(manifest["schema_version"]) is not int
            or manifest["schema_version"] != 1
        ):
            return unavailable("invalid_test_manifest")
        normalized = {}
        root = root.resolve()
        working_directory = (working_directory or root).resolve()
        working_directory.relative_to(root)
        for name, entry in coverage["files"].items():
            if not isinstance(name, str) or "\\" in name or ".." in Path(name).parts:
                raise EvidenceError("Unsafe coverage path")
            path = Path(name)
            candidate = path if path.is_absolute() else working_directory / path
            if candidate.is_symlink() or any(
                parent.is_symlink() for parent in candidate.parents if parent != root
            ):
                raise EvidenceError("Coverage source is a symlink")
            relative = candidate.resolve().relative_to(root).as_posix()
            source_path(relative)
            if (
                relative not in tracked_paths
                or relative in normalized
                or not isinstance(entry, dict)
            ):
                raise EvidenceError("Coverage source is not unique tracked content")
            # Retain only detailed line sets. Context labels, function names,
            # timestamps, metadata and absolute host paths are never copied.
            normalized[relative] = {
                "executed": entry.get("executed_lines"),
                "missing": entry.get("missing_lines"),
                "excluded": entry.get("excluded_lines"),
            }
        value = {
            "schema_version": 1,
            "state": "observed",
            "coverage": normalized,
            "tests": manifest["tests"],
            "dependencies": manifest["dependencies"],
            "runtime": manifest["runtime"],
            "inputs": {
                "source_tree": source_tree,
                "config_sha256": config_sha256,
                "fixture_sha256": fixture_sha256,
                "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
            },
            "measurement_ms": manifest["measurement_ms"]
            + int((time.monotonic() - started) * 1000),
        }
        return validate_observation(value)
    except (EvidenceError, ValueError, TypeError, KeyError):
        return unavailable("invalid_coverage")
