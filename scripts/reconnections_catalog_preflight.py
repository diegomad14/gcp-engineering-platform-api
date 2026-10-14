"""Read-only preflight. Contains no upload, Cloud Run update, IAM write or POST."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse

PROJECT = "cgm-assistant-prod"
REGION = "us-central1"
SERVICE = "eng-platform-api"
ACCOUNT = "cgm-sanplat-deployer@cgm-assistant-prod.iam.gserviceaccount.com"
BASE_SHA = "7a0d21c78e34ce05dbcdc75744c66b6c189624f9c3630cdf40ce6255c5b980a7"
BASE_PATH = "/mnt/eng-platform-catalog/sha256-" + BASE_SHA + ".json"
BASE_IMAGE = "us-central1-docker.pkg.dev/cgm-assistant-prod/cgm-sanplat-repo/eng-platform-api@sha256:156bef4c42087d402c31bc5e02bd62a65afad2f2d4aee7842d6f94c6ae3e22b9"


class ExistingReadError(RuntimeError):
    def __init__(self, command, status):
        self.command = list(command[:3])
        self.status = status
        super().__init__("Existing identity read failed; no access change attempted")


def read_command(*args):
    result = subprocess.run(args, capture_output=True, check=False)
    if result.returncode:
        match = re.search(r"\b(401|403|404)\b", result.stderr.decode(errors="replace"))
        raise ExistingReadError(args, int(match[1]) if match else None)
    return result.stdout


def source_identity(runtime):
    if runtime.get("metadata", {}).get("name") != SERVICE:
        raise ValueError("runtime identity mismatch")
    spec = runtime["spec"]["template"]["spec"]
    containers = spec.get("containers", [])
    if len(containers) != 1 or containers[0].get("image") != BASE_IMAGE:
        raise ValueError("reviewed runtime image changed")
    container = containers[0]
    entries = container.get("env", [])
    if len({row["name"] for row in entries}) != len(entries):
        raise ValueError("duplicate environment variable")
    env = {row["name"]: row.get("value") for row in entries}
    if (
        env.get("ENG_PLATFORM_CATALOG_SHA256") != BASE_SHA
        or env.get("ENG_PLATFORM_CATALOG_PATH") != BASE_PATH
    ):
        raise ValueError("reviewed catalog source changed")
    matches = [
        row
        for row in container.get("volumeMounts", [])
        if row.get("mountPath") == "/mnt/eng-platform-catalog"
    ]
    if len(matches) != 1:
        raise ValueError("exact existing catalog mount required")
    volumes = [
        row
        for row in spec.get("volumes", [])
        if row.get("name") == matches[0].get("name")
    ]
    if len(volumes) != 1:
        raise ValueError("exact existing volume required")
    volume = volumes[0]
    if volume.get("csi", {}).get("driver") == "gcsfuse.run.googleapis.com":
        attrs = volume["csi"].get("volumeAttributes", {})
        bucket = attrs.get("bucketName")
        options = attrs.get("mountOptions", "")
        options = options.split(",") if isinstance(options, str) else options
    elif isinstance(volume.get("gcs"), dict):
        bucket = volume["gcs"].get("bucket")
        options = volume["gcs"].get("mountOptions", [])
    else:
        raise ValueError("unsupported mount; do not guess a bucket")
    if not isinstance(bucket, str) or not bucket or any(x in bucket for x in "/:# \n"):
        raise ValueError("invalid existing bucket")
    prefixes = [
        option.split("=", 1)[1] for option in options if option.startswith("only-dir=")
    ]
    if len(prefixes) > 1 or any(
        part in {".", ".."} for prefix in prefixes for part in prefix.split("/")
    ):
        raise ValueError("unsupported directory mount")
    prefix = prefixes[0].strip("/") + "/" if prefixes and prefixes[0].strip("/") else ""
    return bucket, prefix + "sha256-" + BASE_SHA + ".json"


def main():
    report_path = Path(os.environ["CGM_PREFLIGHT_REPORT"])
    audit = {
        "status": "blocked",
        "authorization": "user granted the exact two reconnections catalog pins",
        "mutation_performed": False,
        "source_sha256": BASE_SHA,
    }
    try:
        audit["stage"] = "verify_existing_identity"
        active = (
            read_command(
                "gcloud",
                "auth",
                "list",
                "--filter=status:ACTIVE",
                "--format=value(account)",
            )
            .decode()
            .strip()
        )
        if active != ACCOUNT:
            raise ValueError("existing identity differs from reviewed principal")
        audit["stage"] = "read_central_runtime"
        runtime = json.loads(
            read_command(
                "gcloud",
                "run",
                "services",
                "describe",
                SERVICE,
                "--project",
                PROJECT,
                "--region",
                REGION,
                "--format=json",
            )
        )
        bucket, key = source_identity(runtime)
        uri = "gs://" + bucket + "/" + key
        audit["stage"] = "read_original_object_metadata"
        metadata = json.loads(
            read_command(
                "gcloud", "storage", "objects", "describe", uri, "--format=json"
            )
        )
        generation = str(metadata.get("generation", ""))
        if (
            not generation.isdigit()
            or not 0 < int(metadata.get("size", 0)) <= 32 * 1024 * 1024
        ):
            raise ValueError("bounded immutable object generation required")
        audit["stage"] = "read_exact_original_generation"
        raw = read_command("gcloud", "storage", "cat", uri + "#" + generation)
        module_path = Path(__file__).with_name("pin_reconnections_catalog.py")
        spec = importlib.util.spec_from_file_location(
            "scoped_catalog_policy", module_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        candidate, proof = module.prepare(raw, BASE_SHA)
        # Local schema selection only; this never changes the Cloud Run environment.
        os.environ["ENG_PLATFORM_MOCK_MODE"] = "true"
        from eng_platform_api.services.log_catalog import validate_catalog

        validate_catalog(module.DECODER.decode(raw.decode("utf-8")))
        validate_catalog(module.DECODER.decode(candidate.decode("utf-8")))
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _project = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        session = AuthorizedSession(credentials)
        audit["stage"] = "inspect_existing_bucket_permissions"
        response = session.get(
            "https://storage.googleapis.com/storage/v1/b/"
            + urllib.parse.quote(bucket, safe="")
            + "/iam/testPermissions",
            params=[
                ("permissions", "storage.objects.get"),
                ("permissions", "storage.objects.create"),
            ],
            timeout=20,
        )
        if response.status_code != 200:
            audit["http_status"] = response.status_code
            raise ValueError("existing bucket permission inspection failed")
        permissions = set(response.json().get("permissions", []))
        audit.update(proof)
        audit.update(
            {
                "status": "read_only_preflight_passed"
                if {"storage.objects.get", "storage.objects.create"}.issubset(
                    permissions
                )
                else "read_only_preflight_create_permission_not_certified",
                "schema_valid": True,
                "existing_account": ACCOUNT,
                "existing_object_read": True,
                "bucket_test_reports_object_create": "storage.objects.create"
                in permissions,
                "conditional_object_permissions_may_require_operator_review": "storage.objects.create"
                not in permissions,
                "bucket_identity_sha256": hashlib.sha256(bucket.encode()).hexdigest(),
                "source_generation": generation,
                "mutation_performed": False,
                "stage": "completed_read_only",
            }
        )
    except Exception as error:
        if isinstance(error, ExistingReadError):
            audit["failed_read_command"] = error.command
            audit["http_status"] = error.status
        audit["error"] = (
            "Read-only preflight could not certify existing access or reviewed source; no permissions were changed"
        )
        raise RuntimeError(audit["error"]) from None
    finally:
        report_path.write_text(json.dumps(audit, indent=2) + "\n")


if __name__ == "__main__":
    main()
