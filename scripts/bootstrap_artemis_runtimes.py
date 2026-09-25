#!/usr/bin/env python3
"""Provision an inert Artemis Cloud Run resource from its reviewed legacy peer.

This is a one-time bootstrap. It never executes Jobs, changes Schedulers or
Cloud Tasks, moves existing traffic, or copies secret values. Normal releases
after bootstrap go through eng-platform using an exact tag and oss-v2 evidence.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PROJECT = "cgm-assistant-prod"
REGION = "us-central1"
SOURCE_BUCKET = "cgm-sanplat-data"
TARGET_BUCKET = "cgm-artemis-data"


@dataclass(frozen=True)
class Runtime:
    kind: str
    source: str
    service_account: str
    worker_type: str = ""


RUNTIMES: dict[str, Runtime] = {
    "cgm-artemis-api": Runtime("services", "cgm-sanplat-api", "artemis-api-runtime"),
    "cgm-artemis-web": Runtime("services", "cgm-sanplat-web", "artemis-web-runtime"),
    "cgm-artemis-job-dispatcher": Runtime(
        "services", "cgm-sanplat-job-dispatcher", "artemis-dispatcher-runtime"
    ),
    "cgm-artemis-job-worker": Runtime(
        "services", "cgm-sanplat-job-worker", "artemis-job-worker-runtime"
    ),
    "cgm-artemis-sync-worker": Runtime(
        "services", "cgm-sanplat-job-worker", "artemis-sync-runtime", "sync"
    ),
    "cgm-artemis-clock-sync-worker": Runtime(
        "services", "cgm-sanplat-job-worker", "artemis-clock-runtime", "clock-sync"
    ),
    "cgm-artemis-data-recovery-worker": Runtime(
        "services", "cgm-sanplat-job-worker", "artemis-datarec-runtime", "data-recovery"
    ),
    "cgm-artemis-fnd-ip-sync-worker": Runtime(
        "services", "cgm-sanplat-job-worker", "artemis-fndip-runtime", "fnd-ip-sync"
    ),
    "cgm-artemis-fnd-observation-worker": Runtime(
        "jobs", "cgm-sanplat-fnd-observation-worker", "artemis-fndobs-runtime"
    ),
    "cgm-artemis-readings-export-worker": Runtime(
        "jobs", "cgm-sanplat-readings-export-worker", "artemis-readings-runtime"
    ),
    "cgm-artemis-smarti-prevention-worker": Runtime(
        "jobs", "cgm-sanplat-smarti-prevention-worker", "artemis-smarti-runtime"
    ),
    "cgm-artemis-wm-sweep-worker": Runtime(
        "jobs", "cgm-sanplat-wm-sweep-worker", "artemis-wmsweep-runtime"
    ),
}


def command(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, capture_output=True, text=True)


def _container_spec(manifest: dict[str, Any], runtime: Runtime) -> dict[str, Any]:
    if runtime.kind == "services":
        return manifest["spec"]["template"]["spec"]
    return manifest["spec"]["template"]["spec"]["template"]["spec"]


def _set_env(container: dict[str, Any], name: str, value: str) -> None:
    rows = container.setdefault("env", [])
    for row in rows:
        if row.get("name") == name:
            row.pop("valueFrom", None)
            row["value"] = value
            return
    rows.append({"name": name, "value": value})


def make_manifest(
    source: dict[str, Any], target: str, available_secrets: set[str]
) -> dict[str, Any]:
    """Clone configuration, replace identities, and reject missing secret aliases."""
    runtime = RUNTIMES[target]
    manifest = copy.deepcopy(source)
    expected_kind = "Service" if runtime.kind == "services" else "Job"
    if (
        manifest.get("kind") != expected_kind
        or manifest.get("metadata", {}).get("name") != runtime.source
    ):
        raise ValueError("source runtime does not match the allowlisted peer")
    metadata = manifest["metadata"]
    metadata["name"] = target
    metadata["annotations"] = {
        key: value
        for key, value in metadata.get("annotations", {}).items()
        if key == "run.googleapis.com/ingress"
    }
    metadata["labels"] = {"managed-by": "eng-platform-bootstrap"}
    template_metadata = manifest["spec"]["template"].setdefault("metadata", {})
    template_metadata.pop("name", None)
    template_metadata["annotations"] = {
        key: value
        for key, value in template_metadata.get("annotations", {}).items()
        if key
        not in {
            "run.googleapis.com/client-name",
            "run.googleapis.com/client-version",
        }
    }
    template_metadata["labels"] = {"managed-by": "eng-platform-bootstrap"}
    if runtime.kind == "services":
        manifest["spec"]["traffic"] = [{"latestRevision": True, "percent": 100}]

    spec = _container_spec(manifest, runtime)
    spec["serviceAccountName"] = (
        f"{runtime.service_account}@{PROJECT}.iam.gserviceaccount.com"
    )
    containers = spec.get("containers", [])
    if len(containers) != 1 or "@sha256:" not in containers[0].get("image", ""):
        raise ValueError("bootstrap requires one immutable source image")
    container = containers[0]
    for volume in spec.get("volumes", []):
        attributes = volume.get("csi", {}).get("volumeAttributes", {})
        if attributes.get("bucketName") == SOURCE_BUCKET:
            attributes["bucketName"] = TARGET_BUCKET
    for row in container.get("env", []):
        secret = row.get("valueFrom", {}).get("secretKeyRef")
        if secret:
            current = secret.get("name", "")
            replacement = (
                current
                if current.startswith("cgm-artemis-")
                else f"cgm-artemis-{current}"
            )
            if replacement not in available_secrets:
                raise ValueError(f"Artemis secret alias is missing: {replacement}")
            secret["name"] = replacement
        elif row.get("value") == SOURCE_BUCKET:
            row["value"] = TARGET_BUCKET
        elif str(row.get("value", "")).startswith(f"gs://{SOURCE_BUCKET}/"):
            row["value"] = row["value"].replace(
                f"gs://{SOURCE_BUCKET}/", f"gs://{TARGET_BUCKET}/", 1
            )
        elif re.search(r"(?:SECRET|PASSWORD|API_KEY)$", row.get("name", "")):
            raise ValueError(f"sensitive literal environment variable: {row['name']}")

    if target == "cgm-artemis-api":
        for name in (
            "APP_BACKGROUND_TASKS_ENABLED",
            "WM_SWEEP_ENABLED",
            "DATA_RECOVERY_CONTINUOUS_ENABLED",
        ):
            _set_env(container, name, "false")
    if target.startswith("cgm-artemis-") and target not in {
        "cgm-artemis-api",
        "cgm-artemis-web",
    }:
        _set_env(container, "JOB_WORKER_PREFIX", "cgm-artemis")
    if runtime.worker_type:
        _set_env(container, "ARTEMIS_WORKER_TYPE", runtime.worker_type)
    if target == "cgm-artemis-job-dispatcher":
        _set_env(
            container,
            "JOB_TASK_OIDC_SERVICE_ACCOUNT",
            f"artemis-tasks-invoker@{PROJECT}.iam.gserviceaccount.com",
        )
    return manifest


def may_repair(existing: dict[str, Any], target: str, expected: dict[str, Any]) -> bool:
    """Allow only a failed bootstrap Service with no ready revision or traffic."""
    if RUNTIMES[target].kind != "services":
        return False
    if (
        existing.get("kind") != "Service"
        or existing.get("metadata", {}).get("name") != target
        or existing.get("metadata", {}).get("labels", {}).get("managed-by")
        != "eng-platform-bootstrap"
    ):
        return False
    status = existing.get("status", {})
    if status.get("latestReadyRevisionName") or status.get("traffic"):
        return False
    if not any(
        row.get("type") == "Ready" and row.get("status") == "False"
        for row in status.get("conditions", [])
    ):
        return False
    current_spec = _container_spec(existing, RUNTIMES[target])
    expected_spec = _container_spec(expected, RUNTIMES[target])
    return current_spec.get("serviceAccountName") == expected_spec.get(
        "serviceAccountName"
    ) and current_spec.get("containers", [{}])[0].get("image") == expected_spec.get(
        "containers", [{}]
    )[0].get("image")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", choices=sorted(RUNTIMES))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--repair", action="store_true")
    args = parser.parse_args()
    runtime = RUNTIMES[args.target]
    base = ["gcloud", "run", runtime.kind]
    flags = [f"--region={REGION}", f"--project={PROJECT}"]
    existing_result = command(
        *base, "describe", args.target, *flags, "--format=json", check=False
    )
    if args.repair and not args.apply:
        raise SystemExit("--repair requires --apply")
    if existing_result.returncode == 0 and not args.repair:
        raise SystemExit(f"{args.target} already exists; bootstrap is create-only")
    if existing_result.returncode != 0 and args.repair:
        raise SystemExit(f"{args.target} does not exist; repair is unavailable")
    result = command(*base, "describe", runtime.source, *flags, "--format=export")
    secrets = command(
        "gcloud", "secrets", "list", f"--project={PROJECT}", "--format=value(name)"
    ).stdout.splitlines()
    manifest = make_manifest(yaml.safe_load(result.stdout), args.target, set(secrets))
    if args.repair and not may_repair(
        json.loads(existing_result.stdout), args.target, manifest
    ):
        raise SystemExit(f"{args.target} is not a failed, traffic-free bootstrap")
    spec = _container_spec(manifest, runtime)
    container = spec["containers"][0]
    summary = {
        "target": args.target,
        "source": runtime.source,
        "kind": runtime.kind,
        "image": container["image"],
        "service_account": spec["serviceAccountName"],
        "secret_references": sorted(
            {
                row["valueFrom"]["secretKeyRef"]["name"]
                for row in container.get("env", [])
                if row.get("valueFrom", {}).get("secretKeyRef")
            }
        ),
        "env_count": len(container.get("env", [])),
        "applied": args.apply,
        "repair": args.repair,
    }
    if args.apply:
        with tempfile.TemporaryDirectory(prefix="artemis-bootstrap-") as directory:
            path = Path(directory) / "runtime.yaml"
            path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
            command(*base, "replace", str(path), *flags, "--quiet")
        live = command(*base, "describe", args.target, *flags, "--format=export")
        actual = yaml.safe_load(live.stdout)
        actual_spec = _container_spec(actual, runtime)
        if (
            actual["metadata"]["name"] != args.target
            or actual_spec["serviceAccountName"] != spec["serviceAccountName"]
            or actual_spec["containers"][0]["image"] != container["image"]
        ):
            raise RuntimeError("created runtime does not match reviewed bootstrap")
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
