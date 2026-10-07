"""Bounded observational evidence. Nothing in this module authorizes test reuse.

Repository-created files are untrusted measurements, even after a control-plane
receipt binds them to an authenticated execution. Matching observations are not
an attestation of hermeticity, dependency pinning, or test execution.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

MAX_BYTES = 700_000
MAX_FILES = 4096
MAX_TESTS = 20_000
MAX_LINES = 300_000
HASH = re.compile(r"^[0-9a-f]{64}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
REASONS = {
    "missing_coverage",
    "invalid_coverage",
    "missing_test_manifest",
    "invalid_test_manifest",
    "payload_too_large",
    "unsupported_runtime",
    "collection_failed",
    "export_failed",
}
CONTEXT_FIELDS = {
    "repository",
    "service_name",
    "head_sha",
    "base_sha",
    "fingerprint",
    "provider",
    "provider_run_id",
    "executor_digest",
    "profile_hash",
    "policy_hash",
    "operation",
}


class EvidenceError(ValueError):
    """Evidence is malformed, oversized, or not bound to its recorded digest."""


def canonical(value: Any, *, limit: int = MAX_BYTES) -> bytes:
    try:
        result = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (ValueError, TypeError, RecursionError) as exc:
        raise EvidenceError("Invalid evidence JSON") from exc
    if len(result) > limit:
        raise EvidenceError("Evidence exceeds byte limit")
    return result


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _object(value: Any, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise EvidenceError("Evidence fields do not match the allowlist")
    return value


def _integer(value: Any, low: int, high: int) -> None:
    if type(value) is not int or not low <= value <= high:
        raise EvidenceError("Evidence integer is out of bounds")


def _hash(value: Any, pattern: re.Pattern[str] = HASH) -> None:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise EvidenceError("Invalid evidence digest")


def source_path(value: Any) -> str:
    """Require canonical repository-relative Python source paths, never host paths."""
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 512
        or not re.fullmatch(r"[A-Za-z0-9_./-]+\.py", value)
        or "\\" in value
        or any(part in {"", ".", "..", ".git"} for part in value.split("/"))
        or PurePosixPath(value).is_absolute()
    ):
        raise EvidenceError("Unsafe evidence source path")
    return value


def validate_observation(value: Any) -> dict[str, Any]:
    canonical(value)
    if isinstance(value, dict) and value.get("state") == "unavailable":
        _object(value, {"schema_version", "state", "reason"})
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or not isinstance(value["reason"], str)
            or value["reason"] not in REASONS
        ):
            raise EvidenceError("Invalid unavailable observation")
        return value
    _object(
        value,
        {
            "schema_version",
            "state",
            "coverage",
            "tests",
            "dependencies",
            "runtime",
            "inputs",
            "measurement_ms",
        },
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["state"] != "observed"
    ):
        raise EvidenceError("Unsupported observation schema")
    coverage = value["coverage"]
    if not isinstance(coverage, dict) or not 1 <= len(coverage) <= MAX_FILES:
        raise EvidenceError("Invalid line coverage file count")
    total_lines = 0
    for path, entry in coverage.items():
        source_path(path)
        _object(entry, {"executed", "missing", "excluded"})
        line_sets = []
        for lines in entry.values():
            if not isinstance(lines, list):
                raise EvidenceError("Invalid coverage lines")
            total_lines += len(lines)
            if total_lines > MAX_LINES:
                raise EvidenceError("Coverage line count exceeds limit")
            for line in lines:
                _integer(line, 1, 10_000_000)
            if lines != sorted(set(lines)):
                raise EvidenceError("Coverage lines must be unique and sorted")
            line_sets.append(set(lines))
        if any(line_sets[a] & line_sets[b] for a, b in ((0, 1), (0, 2), (1, 2))):
            raise EvidenceError("Contradictory line coverage")
    tests = _object(
        value["tests"],
        {
            "items",
            "collection_sha256",
            "collected",
            "deselected",
            "collection_errors",
            "exit_code",
            "complete",
        },
    )
    items = tests["items"]
    if not isinstance(items, list) or len(items) > MAX_TESTS:
        raise EvidenceError("Invalid test manifest size")
    identifiers = []
    for entry in items:
        if not isinstance(entry, list) or len(entry) != 4:
            raise EvidenceError("Invalid test manifest item")
        _hash(entry[0])
        identifiers.append(entry[0])
        for outcome in entry[1:]:
            # 0 absent, 1 passed, 2 failed, 3 skipped, 4 xfailed, 5 xpassed.
            _integer(outcome, 0, 5)
    if identifiers != sorted(set(identifiers)):
        raise EvidenceError("Test identities must be unique and sorted")
    _hash(tests["collection_sha256"])
    if tests["collection_sha256"] != digest(identifiers):
        raise EvidenceError("Test collection digest mismatch")
    _integer(tests["collected"], 0, MAX_TESTS)
    if tests["collected"] != len(items):
        raise EvidenceError("Test collection count mismatch")
    for name in ("deselected", "collection_errors"):
        _integer(tests[name], 0, MAX_TESTS)
    _integer(tests["exit_code"], 0, 255)
    if type(tests["complete"]) is not bool:
        raise EvidenceError("Invalid test completion marker")
    if tests["complete"] and any(
        not item[1] or not item[3] or (item[1] == 1 and not item[2]) for item in items
    ):
        raise EvidenceError("Completed manifest has missing test phases")
    deps = _object(value["dependencies"], {"sha256", "files", "bytes", "complete"})
    _hash(deps["sha256"])
    _integer(deps["files"], 0, 200_000)
    _integer(deps["bytes"], 0, 2_000_000_000)
    if type(deps["complete"]) is not bool:
        raise EvidenceError("Invalid dependency completion marker")
    runtime = _object(value["runtime"], {"implementation", "version", "os", "machine"})
    for name, allowed in (
        ("implementation", {"cpython", "pypy", "other"}),
        ("os", {"linux", "darwin", "win32", "other"}),
        ("machine", {"x86_64", "aarch64", "arm64", "AMD64", "other"}),
    ):
        if not isinstance(runtime[name], str) or runtime[name] not in allowed:
            raise EvidenceError("Invalid runtime enum")
    if not isinstance(runtime["version"], str) or not re.fullmatch(
        r"\d{1,3}\.\d{1,3}\.\d{1,3}", runtime["version"]
    ):
        raise EvidenceError("Invalid runtime version")
    inputs = _object(
        value["inputs"],
        {
            "source_tree",
            "config_sha256",
            "fixture_sha256",
            "command_sha256",
        },
    )
    for name, item in inputs.items():
        _hash(item, SHA if name == "source_tree" else HASH)
    _integer(value["measurement_ms"], 0, 3_600_000)
    return value


def unavailable(reason: str) -> dict[str, Any]:
    return validate_observation(
        {"schema_version": 1, "state": "unavailable", "reason": reason}
    )


def _context(value: Any) -> dict[str, Any]:
    _object(value, CONTEXT_FIELDS)
    for name in ("head_sha", "base_sha"):
        _hash(value[name], SHA)
    for name in ("fingerprint", "profile_hash", "policy_hash"):
        _hash(value[name])
    if not isinstance(value["executor_digest"], str) or not re.fullmatch(
        r"[A-Za-z0-9._:/-]{1,256}@sha256:[0-9a-f]{64}", value["executor_digest"]
    ):
        raise EvidenceError("Executor must use an immutable digest")
    if not isinstance(value["repository"], str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", value["repository"]
    ):
        raise EvidenceError("Invalid repository identity")
    if not isinstance(value["service_name"], str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,126}[a-z0-9]", value["service_name"]
    ):
        raise EvidenceError("Invalid service identity")
    if not isinstance(value["provider_run_id"], str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,128}", value["provider_run_id"]
    ):
        raise EvidenceError("Invalid provider run identity")
    if (
        not isinstance(value["provider"], str)
        or value["provider"] not in {"github_actions", "cloud_build"}
        or not isinstance(value["operation"], str)
        or value["operation"] not in {"pr_quality", "main_release"}
    ):
        raise EvidenceError("Invalid execution identity")
    return value


def seal_observation(
    observation: dict[str, Any],
    execution: dict[str, Any],
    report_hash: str,
    recorded_at: str,
) -> dict[str, Any]:
    """Called by authenticated control-plane code with its own execution record.

    This is a receipt, not an attestation. Never pass repository-supplied identity
    as ``execution`` or trust a receipt submitted by a caller.
    """
    validate_observation(observation)
    _hash(report_hash)
    context = _context({key: execution.get(key, "") for key in CONTEXT_FIELDS})
    value = {
        "schema_version": 1,
        "kind": "test-observation-receipt",
        "trust": "authenticated_receipt_untrusted_measurements",
        "context": context,
        "report_sha256": report_hash,
        "observation_sha256": digest(observation),
        "recorded_at": recorded_at,
        "observation": observation,
    }
    validate_receipt(value)
    return value


def validate_receipt(value: Any) -> dict[str, Any]:
    canonical(value, limit=MAX_BYTES + 4096)
    _object(
        value,
        {
            "schema_version",
            "kind",
            "trust",
            "context",
            "report_sha256",
            "observation_sha256",
            "recorded_at",
            "observation",
        },
    )
    if (
        type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["kind"] != "test-observation-receipt"
        or value["trust"] != "authenticated_receipt_untrusted_measurements"
    ):
        raise EvidenceError("Untrusted receipt kind")
    _context(value["context"])
    for name in ("report_sha256", "observation_sha256"):
        _hash(value[name])
    validate_observation(value["observation"])
    if digest(value["observation"]) != value["observation_sha256"]:
        raise EvidenceError("Observation content digest mismatch")
    try:
        timestamp = datetime.fromisoformat(value["recorded_at"])
        if timestamp.tzinfo is None:
            raise ValueError("missing timezone")
    except (TypeError, ValueError) as exc:
        raise EvidenceError("Invalid receipt timestamp") from exc
    return value


def compare_shadow(
    left: Any,
    right: Any,
    *,
    now: datetime,
    max_age_seconds: int = 86400,
    revoked_fingerprints: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Compare only stored receipts; no caller may use this as an admission check.

    TTL only adds a rejection reason. No value of TTL enables reuse. References
    and provenance must be obtained from the trusted store, not artifact claims.
    """
    if now.tzinfo is None or max_age_seconds < 0:
        raise ValueError("Shadow comparison requires an aware time and nonnegative TTL")
    reasons = {"stage_a_shadow_only", "hermeticity_unproven", "untrusted_measurements"}
    valid = []
    for label, candidate in (("previous", left), ("current", right)):
        if candidate is None:
            reasons.add(f"{label}_evidence_missing")
            continue
        try:
            valid.append(validate_receipt(candidate))
        except EvidenceError:
            reasons.add(f"{label}_evidence_invalid")
            continue
        age = (now - datetime.fromisoformat(candidate["recorded_at"])).total_seconds()
        if not math.isfinite(age) or age < 0 or age > max_age_seconds:
            reasons.add(f"{label}_evidence_stale")
        if candidate["context"]["fingerprint"] in revoked_fingerprints:
            reasons.add(f"{label}_evidence_revoked")
        obs = candidate["observation"]
        if obs["state"] == "unavailable":
            reasons.add(f"{label}_{obs['reason']}")
        else:
            tests = obs["tests"]
            if not obs["dependencies"]["complete"]:
                reasons.add(f"{label}_dependency_content_incomplete")
            if (
                not tests["complete"]
                or tests["exit_code"]
                or tests["collection_errors"]
                or not tests["items"]
                or any(2 in item[1:] or 5 in item[1:] for item in tests["items"])
            ):
                reasons.add(f"{label}_tests_incomplete_or_failed")
            if tests["deselected"] or any(
                3 in item[1:] or 4 in item[1:] for item in tests["items"]
            ):
                reasons.add(f"{label}_tests_skipped_or_deselected")
    if len(valid) == 2:
        old, new = valid
        for key in CONTEXT_FIELDS - {
            "fingerprint",
            "provider_run_id",
            "operation",
            "provider",
        }:
            if old["context"][key] != new["context"][key]:
                reasons.add(f"{key}_mismatch")
        if old["context"]["fingerprint"] == new["context"]["fingerprint"]:
            reasons.add("same_execution_replay")
        if old["observation"]["state"] == new["observation"]["state"] == "observed":
            for key in ("coverage", "tests", "dependencies", "runtime"):
                if old["observation"][key] != new["observation"][key]:
                    reasons.add(f"{key}_mismatch")
            for key in old["observation"]["inputs"]:
                if (
                    old["observation"]["inputs"][key]
                    != new["observation"]["inputs"][key]
                ):
                    reasons.add(f"{key}_mismatch")
    return {
        "mode": "shadow",
        "eligible": False,
        "reuse_allowed": False,
        "reasons": sorted(reasons),
    }
