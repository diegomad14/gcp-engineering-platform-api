#!/usr/bin/env python3
"""Validate mandatory evidence before publication and report ordinary failures."""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
from pathlib import Path


REQUIRED_STEPS = ("source", "tooling", "postgres", "runtime", "quality")


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def request_errors(workspace: Path, request: dict) -> list[str]:
    errors = []
    unsigned = {
        key: value for key, value in request.items() if key != "request_fingerprint"
    }
    fingerprint = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if fingerprint != request.get("request_fingerprint"):
        errors.append("Request fingerprint does not match its contents")
    expected = os.environ.get("FALLBACK_REQUEST_FINGERPRINT")
    if not expected or fingerprint != expected:
        errors.append("Request fingerprint does not match the Cloud Build substitution")
    hashes = request.get("input_hashes", {})
    if not isinstance(hashes, dict) or not hashes:
        return [*errors, "Missing input hashes"]
    for filename, digest in hashes.items():
        path = workspace / filename
        if Path(filename).is_absolute() or ".." in Path(filename).parts:
            errors.append("Input path escapes the build workspace")
            continue
        try:
            matches = hashlib.sha256(path.read_bytes()).hexdigest() == digest
        except OSError:
            matches = False
        if not matches:
            errors.append(f"Input was changed or is missing: {filename}")
    return errors


def canonical_quality_errors(workspace: Path, quality: dict) -> list[str]:
    # These modules are copied verbatim from the central checkout at preparation.
    # Verification runs in the pinned tooling container, with Pydantic installed.
    from pydantic import ValidationError
    from eng_platform_api.models import CatalogService, QualityReportCreate
    from eng_platform_api.services.quality_policy import policy_errors

    try:
        report = QualityReportCreate.model_validate(quality)
        service = CatalogService.model_validate(
            read_json(workspace / "catalog-service.json")
        )
    except (ValidationError, ValueError, TypeError):
        return ["Quality report or central catalog schema is invalid"]
    return policy_errors(report, service)


def errors_for(workspace: Path, *, published: bool = False) -> list[str]:
    errors = []
    evidence = workspace / "evidence"
    required = (*REQUIRED_STEPS, "verify", "publish") if published else REQUIRED_STEPS
    for step in required:
        result = read_json(evidence / f"{step}.json")
        if result.get("step") != step or result.get("exit_code") != 0:
            errors.append(f"Missing or failed step: {step}")
    request = read_json(workspace / "request.json")
    errors.extend(request_errors(workspace, request))
    quality = read_json(workspace / "quality-report.json")
    for field in (
        "repository",
        "service_name",
        "commit_sha",
        "base_sha",
        "policy_version",
        "profile",
    ):
        if not request.get(field) or quality.get(field) != request[field]:
            errors.append(f"Quality identity mismatch: {field}")
    if not published:
        errors.extend(canonical_quality_errors(workspace, quality))
    if published:
        for field, variable in (
            ("commit_sha", "FALLBACK_RELEASE_SHA"),
            ("report_uri", "FALLBACK_QUALITY_URI"),
            ("summary_uri", "FALLBACK_SUMMARY_URI"),
            ("platform_sha", "FALLBACK_PLATFORM_SHA"),
            ("project_id", "FALLBACK_PROJECT_ID"),
        ):
            if request.get(field) != os.environ.get(variable):
                errors.append(f"Request does not match build metadata: {field}")
        if not os.environ.get("FALLBACK_BUILD_ID"):
            errors.append("Cloud Build ID is missing")
        try:
            approved = (evidence / "approved").read_text() == "yes"
        except OSError:
            approved = False
        if not approved:
            errors.append("Canonical quality verification did not approve publication")
        try:
            approved_hash = (evidence / "verified-quality-report.sha256").read_text()
            current_hash = hashlib.sha256(
                (workspace / "quality-report.json").read_bytes()
            ).hexdigest()
        except OSError:
            approved_hash, current_hash = "", "missing"
        if approved_hash != current_hash:
            errors.append("Quality report changed after canonical verification")
        try:
            digest = (evidence / "image-digest.txt").read_text().strip()
        except OSError:
            digest = ""
        image_repository = request.get("image", "").rsplit(":", 1)[0]
        if not re.fullmatch(
            re.escape(image_repository) + r"@sha256:[0-9a-f]{64}", digest
        ):
            errors.append("Published image digest is missing")
    return errors


def main(workspace: Path, mode: str) -> int:
    evidence = workspace / "evidence"
    evidence.mkdir(exist_ok=True)
    errors = errors_for(workspace, published=mode == "finalize")
    request = read_json(workspace / "request.json")
    try:
        digest_reference = (evidence / "image-digest.txt").read_text().strip()
    except OSError:
        digest_reference = ""
    try:
        quality_hash = hashlib.sha256(
            (workspace / "quality-report.json").read_bytes()
        ).hexdigest()
    except OSError:
        quality_hash = ""
    summary = {
        **{
            key: request.get(key)
            for key in (
                "repository",
                "service_name",
                "commit_sha",
                "base_sha",
                "profile",
                "build_profile",
                "machine_type",
                "workers",
                "attempt_id",
                "request_fingerprint",
                "image",
                "report_uri",
                "summary_uri",
                "platform_sha",
            )
        },
        "build_id": os.environ.get("FALLBACK_BUILD_ID", ""),
        "project_id": os.environ.get("FALLBACK_PROJECT_ID", ""),
        "digest": digest_reference.rsplit("@", 1)[-1]
        if "@" in digest_reference
        else "",
        "image_digest": digest_reference,
        "quality_report_sha256": quality_hash,
        "passed": not errors,
        "errors": errors,
        "steps": [
            read_json(evidence / f"{step}.json")
            for step in (*REQUIRED_STEPS, "verify", "publish")
        ],
    }
    (evidence / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    if mode == "verify":
        (evidence / "approved").write_text("no" if errors else "yes")
        (evidence / "verified-quality-report.sha256").write_text(
            quality_hash if not errors else ""
        )
    else:
        (evidence / "result").write_text("failed" if errors else "passed")
    print(f"Fallback {mode}: {'failed' if errors else 'passed'} ({len(errors)} errors)")
    for error in errors:
        print(error)
    # Finalize must upload the failure evidence before its shell exits nonzero.
    return int(bool(errors)) if mode == "verify" else 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in {"verify", "finalize"}:
        raise SystemExit("Expected verify or finalize")
    raise SystemExit(main(Path("/workspace"), sys.argv[1]))
