#!/usr/bin/env python3
"""Submit once, reconcile uncertain submissions, and inspect an existing build."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request


def read(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object")
    return value


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".fallback-state-")
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        yield


def cloud(*args: str) -> object:
    completed = subprocess.run(
        ["gcloud", *args, "--format=json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
    )
    return json.loads(completed.stdout)


def validate_inputs(directory: Path) -> dict:
    directory = directory.resolve()
    entries = (directory / "input-manifest.sha256").read_text().splitlines()
    listed = set()
    for entry in entries:
        digest, relative = entry.split("  ", 1)
        raw_path = directory / relative
        path = raw_path.resolve()
        if not path.is_relative_to(directory) or raw_path.is_symlink():
            raise ValueError("Unsafe input manifest")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError(f"Prepared input changed: {relative}")
        listed.add(relative)
    actual = {
        str(path.relative_to(directory))
        for path in directory.rglob("*")
        if path.is_file() and path.name != "input-manifest.sha256"
    }
    if listed != actual:
        raise ValueError("Prepared files were added or removed")
    request = read(directory / "request.json")
    config = read(directory / "cloudbuild.json")
    if (
        config.get("substitutions", {}).get("_REQUEST_FINGERPRINT")
        != request["request_fingerprint"]
    ):
        raise ValueError("Build fingerprint does not match request")
    return request


def location(request: dict) -> list[str]:
    return ["--project=" + request["project_id"], "--region=" + request["region"]]


def matching(build: dict, request: dict) -> bool:
    return (
        build.get("substitutions", {}).get("_REQUEST_FINGERPRINT")
        == request["request_fingerprint"]
    )


def reconcile(request: dict, state: dict, state_file: Path) -> dict:
    if state.get("build_id"):
        build = cloud("builds", "describe", state["build_id"], *location(request))
        if not isinstance(build, dict) or not matching(build, request):
            raise RuntimeError("Build identity does not match the prepared request")
    else:
        builds = cloud(
            "builds",
            "list",
            *location(request),
            "--filter=substitutions._REQUEST_FINGERPRINT="
            + request["request_fingerprint"],
            "--limit=100",
        )
        matches = [
            item
            for item in builds
            if isinstance(item, dict) and matching(item, request)
        ]
        if len(matches) != 1:
            raise RuntimeError(
                "Submission is uncertain; no automatic resubmission. "
                f"Found {len(matches)} matching builds; reconcile Cloud Build before further action."
            )
        build = matches[0]
    state.update(build_id=build["id"], status=build["status"])
    save(state_file, state)
    save(state_file.with_suffix(".build.json"), build)
    return build


def reserve(
    experiment: Path | None, slot: str | None, request: dict, state_file: Path
) -> None:
    if experiment is None:
        return
    if slot not in {"performance", "economy", "confirmation"}:
        raise ValueError(
            "Experiment requires a performance, economy or confirmation slot"
        )
    if slot != "confirmation" and slot != request["build_profile"]:
        raise ValueError("Experiment slot does not match machine profile")
    with lock(experiment):
        ledger = read(experiment) if experiment.exists() else {}
        if slot in ledger:
            raise RuntimeError(
                "Experiment slot already consumed; inspect its existing build"
            )
        if any(
            item["request_fingerprint"] == request["request_fingerprint"]
            for item in ledger.values()
        ):
            raise RuntimeError(
                "Prepared attempt already reserved; confirmation requires a fresh preparation"
            )
        ledger[slot] = {
            "request_fingerprint": request["request_fingerprint"],
            "state_file": str(state_file),
            "commit_sha": request["commit_sha"],
            "base_sha": request["base_sha"],
            "build_profile": request["build_profile"],
        }
        previous = list(ledger.values())
        if any(
            item["commit_sha"] != request["commit_sha"]
            or item["base_sha"] != request["base_sha"]
            for item in previous
        ):
            raise ValueError(
                "All experiment builds must use the same SHA and comparison base"
            )
        save(experiment, ledger)


def operate(
    directory: Path,
    state_file: Path,
    *,
    submit: bool,
    experiment: Path | None = None,
    slot: str | None = None,
) -> dict:
    directory, state_file = directory.resolve(), state_file.resolve()
    if state_file.is_relative_to(directory):
        raise ValueError("State must be outside the immutable build directory")
    if experiment is not None and experiment.resolve().is_relative_to(directory):
        raise ValueError(
            "Experiment ledger must be outside the immutable build directory"
        )
    request = validate_inputs(directory)
    # A different --state-file must not create a second build of this attempt.
    # The caller's file is a convenient mirror; submission ownership is canonical.
    mirror = state_file
    state_file = directory.with_name(directory.name + ".submission.json")
    with lock(state_file):
        if mirror.exists():
            mirrored = read(mirror)
            if mirrored.get("request_fingerprint") != request["request_fingerprint"]:
                raise ValueError("State belongs to another prepared build")
            if not state_file.exists():
                save(state_file, mirrored)
        if state_file.exists():
            state = read(state_file)
            if state.get("request_fingerprint") != request["request_fingerprint"]:
                raise ValueError("State belongs to another prepared build")
            build = reconcile(request, state, state_file)
            save(mirror, read(state_file))
            save(mirror.with_suffix(".build.json"), build)
            return build
        if not submit:
            raise ValueError("No saved submission; inspect the correct state file")
        reserve(experiment, slot, request, state_file)
        state = {
            "status": "SUBMISSION_PENDING",
            "request_fingerprint": request["request_fingerprint"],
        }
        # Persist intent before the external call. Errors/timeouts retain this marker.
        save(state_file, state)
        save(mirror, state)
        build = cloud(
            "builds",
            "submit",
            str(directory),
            "--config=" + str(directory / "cloudbuild.json"),
            "--async",
            *location(request),
        )
        if (
            not isinstance(build, dict)
            or not build.get("id")
            or not matching(build, request)
        ):
            raise RuntimeError("Uncertain submission response; use status to reconcile")
        state.update(build_id=build["id"], status=build["status"])
        save(state_file, state)
        save(state_file.with_suffix(".build.json"), build)
        save(mirror, state)
        save(mirror.with_suffix(".build.json"), build)
        return build


def register_quality(directory: Path, build: dict, api_url: str) -> dict:
    """Register the verified report without running its suite a second time."""
    parsed = urllib.parse.urlparse(api_url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Quality API requires an HTTPS origin")
    token = os.environ.get("QUALITY_API_TOKEN", "")
    if not token:
        raise ValueError("Set QUALITY_API_TOKEN in the environment")
    request = validate_inputs(directory)
    if build.get("status") != "SUCCESS" or not matching(build, request):
        raise ValueError("Only the successful matching build can publish evidence")

    def storage_bytes(uri: str) -> bytes:
        return subprocess.run(
            ["gcloud", "storage", "cat", uri], check=True, capture_output=True
        ).stdout

    summary = json.loads(storage_bytes(request["summary_uri"]))
    report_bytes = storage_bytes(request["report_uri"])
    report = json.loads(report_bytes)
    if (
        summary.get("passed") is not True
        or summary.get("build_id") != build["id"]
        or summary.get("request_fingerprint") != request["request_fingerprint"]
        or summary.get("quality_report_sha256")
        != hashlib.sha256(report_bytes).hexdigest()
    ):
        raise ValueError("Published evidence identity or hash mismatch")
    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
    from eng_platform_api.models import CatalogService, QualityReportCreate
    from eng_platform_api.services.quality_policy import policy_errors

    for key in ("repository", "service_name", "commit_sha", "base_sha"):
        if report.get(key) != request[key]:
            raise ValueError("Report does not match the prepared release")
    errors = policy_errors(
        QualityReportCreate.model_validate(report),
        CatalogService.model_validate(read(directory / "catalog-service.json")),
    )
    if errors:
        raise ValueError("Quality policy rejected report: " + "; ".join(errors))
    report["workflow_run_url"] = build["logUrl"]
    message = urllib.request.Request(
        api_url.rstrip("/") + "/api/quality/reports",
        data=json.dumps(report).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token,
        },
    )
    with urllib.request.urlopen(message, timeout=30) as response:
        registered = json.load(response)
    if registered.get("quality_gate_status") != "PASSED" or any(
        registered.get(key) != request[key]
        for key in ("repository", "service_name", "commit_sha", "base_sha")
    ):
        raise RuntimeError("Platform did not accept the exact quality evidence")
    return {
        key: registered.get(key)
        for key in (
            "service_name",
            "commit_sha",
            "quality_gate_status",
            "workflow_run_url",
        )
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("submit", "status", "register-quality"))
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--experiment", type=Path)
    parser.add_argument("--slot", choices=("performance", "economy", "confirmation"))
    parser.add_argument("--quality-api-url")
    args = parser.parse_args()
    build = operate(
        args.build_dir,
        args.state_file,
        submit=args.mode == "submit",
        experiment=args.experiment,
        slot=args.slot,
    )
    if args.mode == "register-quality":
        if not args.quality_api_url:
            raise ValueError("--quality-api-url is required")
        print(
            json.dumps(
                register_quality(args.build_dir.resolve(), build, args.quality_api_url),
                indent=2,
            )
        )
        return
    print(
        json.dumps(
            {
                key: build.get(key)
                for key in (
                    "id",
                    "status",
                    "logUrl",
                    "createTime",
                    "startTime",
                    "finishTime",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
