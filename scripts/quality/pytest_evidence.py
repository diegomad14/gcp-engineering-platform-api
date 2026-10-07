"""Read-only pytest hooks for bounded, hashed Stage A observations.

No hook changes items, markers, outcomes, or pytest's exit status. This plugin
runs as repository code and its output is explicitly not a trusted attestation.
No node-id text, assertion text, environment dump or dependency URL is exported.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import stat
import sys
import time
from pathlib import Path

MAX_TESTS = 20_000
MAX_BYTES = 600_000
MAX_DEPENDENCY_BYTES = 256_000_000
MAX_DEPENDENCY_FILES = 100_000
MAX_SECONDS = 3.0
_items: dict[str, list[int]] = {}
_deselected = 0
_collection_errors = 0
_complete = True
_started = 0.0
# A nested pytest process must not win the parent's exclusive artifact write.
# Consume the destination before test collection/imports can launch children.
_destination = os.environ.pop("ENG_PLATFORM_TEST_MANIFEST", "")
_owner_session = None
_nested_depth = 0


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _identifier(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def pytest_sessionstart(session):
    global _items, _deselected, _collection_errors, _complete, _started
    global _owner_session, _nested_depth
    if _owner_session is not None:
        # pytest.main() can create another session in this same interpreter.
        # Its results must not replace the root collection or artifact.
        _nested_depth += 1
        _complete = False
        return
    _owner_session = session
    _items = {}
    _deselected = 0
    _collection_errors = 0
    _complete = True
    _started = time.monotonic()


def pytest_collection_finish(session):
    global _complete
    if session is not _owner_session:
        return
    for item in session.items[:MAX_TESTS]:
        key = _identifier(item.nodeid)
        if key in _items:
            _complete = False
        _items[key] = [0, 0, 0]
    if len(session.items) > MAX_TESTS:
        _complete = False


def pytest_deselected(items):
    global _deselected
    if _nested_depth:
        return
    _deselected = min(MAX_TESTS, _deselected + len(items))


def pytest_collectreport(report):
    global _collection_errors
    if _nested_depth:
        return
    if report.failed:
        _collection_errors = min(MAX_TESTS, _collection_errors + 1)


def pytest_runtest_logreport(report):
    global _complete
    if _nested_depth:
        return
    key = _identifier(report.nodeid)
    if key not in _items or report.when not in {"setup", "call", "teardown"}:
        _complete = False
        return
    phase = {"setup": 0, "call": 1, "teardown": 2}[report.when]
    outcome = {"passed": 1, "failed": 2, "skipped": 3}.get(report.outcome, 0)
    if hasattr(report, "wasxfail"):
        outcome = 4 if report.skipped else 5
    if _items[key][phase]:
        # Reruns and duplicate reports require a richer schema; never hide them.
        _complete = False
    _items[key][phase] = outcome


def _dependencies() -> dict:
    """Hash actual installed file bytes, bounded by count, bytes and wall time.

    RECORD claims are not trusted; their file content is read and hashed. Paths,
    names, versions and direct_url metadata are never returned. Symlinks,
    absolute/traversing RECORD entries, editable installs, missing files and a
    resource cap all make this observation incomplete. Native runtime/system
    libraries and network effects remain outside this observational digest.
    """
    started = time.monotonic()
    records = []
    count = size = 0
    complete = True
    try:
        distributions = list(importlib.metadata.distributions())
        for dist in distributions:
            files = dist.files
            if not files:
                complete = False
                continue
            for item in sorted(files, key=str):
                name = str(item)
                # This also marks console-script RECORD traversals incomplete.
                # Stage A favors honest incomplete data over unsafe host reads.
                if Path(name).is_absolute() or ".." in Path(name).parts:
                    complete = False
                    continue
                if name.endswith("direct_url.json"):
                    complete = False  # editable/direct-source provenance unresolved
                if (
                    time.monotonic() - started > MAX_SECONDS
                    or count >= MAX_DEPENDENCY_FILES
                    or size >= MAX_DEPENDENCY_BYTES
                ):
                    return {
                        "sha256": _digest(sorted(records)),
                        "files": count,
                        "bytes": size,
                        "complete": False,
                    }
                path = Path(dist.locate_file(item))
                try:
                    if any(parent.is_symlink() for parent in [path, *path.parents]):
                        complete = False
                        continue
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, "rb") as handle:
                        metadata = os.fstat(handle.fileno())
                        if (
                            not stat.S_ISREG(metadata.st_mode)
                            or size + metadata.st_size > MAX_DEPENDENCY_BYTES
                        ):
                            complete = False
                            continue
                        file_digest = hashlib.sha256()
                        read_size = 0
                        while chunk := handle.read(131072):
                            size += len(chunk)
                            read_size += len(chunk)
                            if (
                                size > MAX_DEPENDENCY_BYTES
                                or time.monotonic() - started > MAX_SECONDS
                            ):
                                return {
                                    "sha256": _digest(sorted(records)),
                                    "files": count,
                                    "bytes": min(size, MAX_DEPENDENCY_BYTES),
                                    "complete": False,
                                }
                            file_digest.update(chunk)
                        if read_size != metadata.st_size:
                            complete = False
                        # Only hashed labels are materialized outside this plugin.
                        records.append(
                            [
                                _identifier(
                                    str(dist.metadata.get("Name", ""))
                                    + "\0"
                                    + str(dist.version)
                                    + "\0"
                                    + name
                                ),
                                file_digest.hexdigest(),
                            ]
                        )
                        count += 1
                except (OSError, ValueError):
                    complete = False
    except Exception:
        complete = False
    return {
        "sha256": _digest(sorted(records)),
        "files": count,
        "bytes": size,
        "complete": complete,
    }


def pytest_sessionfinish(session, exitstatus):
    global _nested_depth
    if session is not _owner_session:
        _nested_depth = max(0, _nested_depth - 1)
        return
    # Best-effort diagnostics must never change pytest's exit code or collection.
    destination = _destination
    if not destination:
        return
    try:
        started = time.monotonic()
        identifiers = sorted(_items)
        runtime = {
            "implementation": sys.implementation.name,
            "version": platform.python_version(),
            "os": sys.platform,
            "machine": platform.machine(),
        }
        for key, allowed in (
            ("implementation", {"cpython", "pypy"}),
            ("os", {"linux", "darwin", "win32"}),
            ("machine", {"x86_64", "aarch64", "arm64", "AMD64"}),
        ):
            if runtime[key] not in allowed:
                runtime[key] = "other"
        value = {
            "schema_version": 1,
            "tests": {
                "items": [[key, *_items[key]] for key in identifiers],
                "collection_sha256": _digest(identifiers),
                "collected": len(identifiers),
                "deselected": _deselected,
                "collection_errors": _collection_errors,
                "exit_code": int(exitstatus),
                "complete": _complete
                and all(phases[0] and phases[2] for phases in _items.values()),
            },
            "dependencies": _dependencies(),
            "runtime": runtime,
            "measurement_ms": int((time.monotonic() - started) * 1000),
        }
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        if len(payload) > MAX_BYTES:
            return
        fd = os.open(
            destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
    except Exception:
        return
