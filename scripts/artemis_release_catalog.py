"""Inspect or activate only the twelve approved Artemis CI/release pins.

Private catalog bytes remain in memory. The existing qualified rollout supplies
CAS updates, a zero-traffic candidate, smoke checks and guarded traffic rollback.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from scripts import reconnections_catalog_activate as rollout
from scripts import reconnections_catalog_preflight as existing
from scripts import pin_artemis_release_catalog as policy

SOURCE_SHA = "b82cdad9d13a881b25b777d5f9f0cb76f909c17e9cb11e823805b13d88da3c1d"
SOURCE_GENERATION = "1791998584920407"
SOURCE_REVISION = "eng-platform-api-cfg-" + SOURCE_SHA[:12]
ROUTING_KEYS = (
    "ENG_PLATFORM_PRIVATE_EXECUTOR_MODE",
    "ENG_PLATFORM_DEPLOY_EXECUTOR_MODE",
    "ENG_PLATFORM_CLOUD_BUILD_ONLY_SERVICES",
    "ENG_PLATFORM_CLOUD_BUILD_ENABLED_SERVICES",
    "ENG_PLATFORM_CLOUD_BUILD_NEW_BUILDS_ENABLED",
    "ENG_PLATFORM_GITHUB_FIRST_SERVICES",
)


def configure(candidate_sha):
    if not re.fullmatch(r"[0-9a-f]{64}", candidate_sha):
        raise ValueError("exact inspected candidate SHA256 required")
    existing.BASE_SHA = SOURCE_SHA
    existing.BASE_PATH = "/mnt/eng-platform-catalog/sha256-" + SOURCE_SHA + ".json"
    rollout.CANDIDATE_SHA = candidate_sha
    rollout.OLD_REVISION = SOURCE_REVISION
    rollout.REVISION = "eng-platform-api-cfg-" + candidate_sha[:12]
    rollout.TAG = "catalog-" + candidate_sha[:12]


def inspect():
    active = (
        existing.read_command(
            "gcloud",
            "auth",
            "list",
            "--filter=status:ACTIVE",
            "--format=value(account)",
        )
        .decode()
        .strip()
    )
    if active != existing.ACCOUNT:
        raise ValueError("existing reviewed identity required")
    runtime = json.loads(
        existing.read_command(
            "gcloud",
            "run",
            "services",
            "describe",
            existing.SERVICE,
            "--project",
            existing.PROJECT,
            "--region",
            existing.REGION,
            "--format=json",
        )
    )
    bucket, key = existing.source_identity(runtime)
    uri = "gs://" + bucket + "/" + key
    metadata = json.loads(
        existing.read_command(
            "gcloud",
            "storage",
            "objects",
            "describe",
            uri,
            "--format=json",
        )
    )
    if str(metadata.get("generation")) != SOURCE_GENERATION or not (
        0 < int(metadata.get("size", 0)) <= 32 * 1024 * 1024
    ):
        raise ValueError("reviewed bounded immutable source generation required")
    raw = existing.read_command(
        "gcloud", "storage", "cat", uri + "#" + SOURCE_GENERATION
    )
    candidate, proof = policy.prepare(raw)
    # Validation only: no runtime environment or source file is changed.
    os.environ["ENG_PLATFORM_MOCK_MODE"] = "true"
    from eng_platform_api.services.log_catalog import validate_catalog

    validate_catalog(json.loads(raw))
    validate_catalog(json.loads(candidate))
    environment = {
        row["name"]: row.get("value")
        for row in runtime["spec"]["template"]["spec"]["containers"][0].get("env", [])
    }
    proof.update(
        source_generation=SOURCE_GENERATION,
        existing_account=active,
        existing_runtime_image=existing.BASE_IMAGE,
        bucket_identity_sha256=hashlib.sha256(bucket.encode()).hexdigest(),
        schema_valid=True,
        routing_environment={key: environment.get(key) for key in ROUTING_KEYS},
        routing_environment_changed=False,
        provider_precedence="explicit release.executor is returned before private policy, deployment executor, circuit or cloud_build_only_services",
        mutation_performed=False,
    )
    return candidate, proof, bucket, key


def publish(candidate, proof, bucket, key):
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    from google.cloud import storage
    from google.api_core.exceptions import PreconditionFailed

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    blob = (
        storage.Client(project=existing.PROJECT, credentials=credentials)
        .bucket(bucket)
        .blob(
            key.removesuffix("sha256-" + SOURCE_SHA + ".json")
            + "sha256-"
            + proof["candidate_sha256"]
            + ".json"
        )
    )

    def upload():
        try:
            blob.upload_from_string(
                candidate,
                content_type="application/json",
                if_generation_match=0,
                timeout=30,
            )
        except PreconditionFailed:
            pass
        blob.reload(timeout=20)
        if (
            blob.download_as_bytes(if_generation_match=blob.generation, timeout=30)
            != candidate
        ):
            raise ValueError("immutable candidate object differs")
        proof["candidate_object_generation"] = str(blob.generation)

    proof["production_write_attempted"] = True
    proof["mutation_performed"] = (
        None  # An uncertain write is never reported as absent.
    )
    rollout.activate(
        rollout.ExistingCloud(AuthorizedSession(credentials)), upload, proof
    )
    proof["mutation_performed"] = True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("inspect", "activate"), required=True)
    parser.add_argument("--candidate-sha256", default="")
    args = parser.parse_args()
    path = Path(os.environ["CGM_ACTIVATION_REPORT"])
    audit = {"status": "blocked", "mutation_performed": False}
    try:
        configure(args.candidate_sha256 if args.mode == "activate" else "0" * 64)
        candidate, audit, bucket, key = inspect()
        if args.mode == "activate":
            if audit["candidate_sha256"] != args.candidate_sha256:
                raise ValueError("candidate differs from inspected approved SHA256")
            publish(candidate, audit, bucket, key)
        else:
            audit["status"] = "read_only_preflight_passed"
    except Exception as error:
        audit["status"] = "blocked"
        audit["error_type"] = type(error).__name__
        if isinstance(error, (existing.ExistingReadError, rollout.CloudError)):
            audit["http_status"] = error.status
        raise RuntimeError(
            "Artemis release-only catalog operation stopped; inspect bounded audit before retry"
        ) from None
    finally:
        path.write_text(json.dumps(audit, indent=2) + "\n")


if __name__ == "__main__":
    main()
