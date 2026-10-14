"""Inspect or activate only the twenty reviewed GitHub executor defaults.

Private catalog bytes remain in memory. The existing qualified rollout supplies
CAS updates, a zero-traffic candidate, smoke checks and guarded traffic rollback.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from scripts import reconnections_catalog_activate as rollout
from scripts import reconnections_catalog_preflight as existing
from scripts import pin_github_defaults_catalog as policy

SOURCE_SHA = "fcfe13a685cd607004b6f132d476b66f550cf8b8b72ae22d0825375abb9a1c17"
SOURCE_GENERATION = "1792010359557854"
SOURCE_REVISION = "eng-platform-api-cfg-" + SOURCE_SHA[:12]
ROUTING_KEYS = (
    "ENG_PLATFORM_PRIVATE_EXECUTOR_MODE",
    "ENG_PLATFORM_DEPLOY_EXECUTOR_MODE",
    "ENG_PLATFORM_CLOUD_BUILD_ONLY_SERVICES",
    "ENG_PLATFORM_CLOUD_BUILD_ENABLED_SERVICES",
    "ENG_PLATFORM_CLOUD_BUILD_NEW_BUILDS_ENABLED",
    "ENG_PLATFORM_GITHUB_FIRST_SERVICES",
)

REPOSITORIES_ENV = "ENG_PLATFORM_CLOUD_BUILD_REPOSITORIES_JSON"
RELEASE_METADATA_KEYS = (
    "ENG_PLATFORM_RELEASE_ENABLED_SERVICES",
    "ENG_PLATFORM_RELEASE_CANARY_SERVICES",
    "ENG_PLATFORM_RELEASE_ORCHESTRATOR_ENABLED",
)
IMAGE_METADATA_KEYS = (
    "ENG_PLATFORM_QUALITY_NODE_IMAGE",
    "ENG_PLATFORM_QUALITY_PYTHON_IMAGE",
    "ENG_PLATFORM_QUALITY_GO_IMAGE",
    "ENG_PLATFORM_RELEASE_PLANNER_IMAGE",
    "ENG_PLATFORM_RELEASE_EXECUTOR_IMAGE",
    "ENG_PLATFORM_CLOUD_BUILD_EXECUTOR_IMAGE",
)
TRIGGER_FORMAT = "json(name,github.owner,github.name,github.push.branch,github.push.tag,repositoryEventConfig.repository,repositoryEventConfig.push.branch,repositoryEventConfig.push.tag,triggerTemplate.repoName,triggerTemplate.branchName,triggerTemplate.tagName,disabled)"
CONNECTED_PATTERN = r"projects/[a-z0-9-]+/locations/[a-z0-9-]+/connections/[A-Za-z0-9_-]+/repositories/[A-Za-z0-9_-]+"
AUDITED_TRIGGER_RESOURCES = (
    ("cgm-assistant-prod", "global"),
    ("cgm-assistant-prod", "us-central1"),
    ("cgm-reconnections-prod", "global"),
    ("cgm-reconnections-prod", "us-central1"),
)
PROJECT_ALIASES = {
    "546821492326": "cgm-assistant-prod",
    "305411509341": "cgm-reconnections-prod",
}
SAFE_ERRORS = {
    "existing reviewed identity required": "identity_mismatch",
    "source differs from reviewed catalog SHA256": "source_hash_mismatch",
    "reviewed managed service must remain enabled": "managed_mode_or_enabled",
    "managed repository identity mismatch": "repository_mismatch",
    "reviewed executor baseline changed": "executor_baseline",
    "exact reviewed twenty two edits required": "edit_count",
    "reviewed bounded immutable source generation required": "source_metadata",
    "reviewed runtime image changed": "runtime_image",
    "reviewed catalog source changed": "catalog_source",
    "exact twenty managed services required": "managed_scope",
}
REPOSITORY_ALIASES = {
    "diegomad14/cgm-sanplat-api": "diegomad14/cgm-artemis-api",
    "diegomad14/cgm-sanplat-web": "diegomad14/cgm-artemis-web",
}


def connected_resource(reference):
    if not isinstance(reference, str) or not re.fullmatch(CONNECTED_PATTERN, reference):
        return None
    parts = reference.split("/")
    return PROJECT_ALIASES.get(parts[1], parts[1]), parts[3]


def canonical_repository(repository):
    normalized = repository.casefold()
    return REPOSITORY_ALIASES.get(normalized, normalized)


def runtime_metadata(environment):
    result = {key: environment.get(key) for key in RELEASE_METADATA_KEYS}
    result["evidence_bucket_metadata"] = {
        key: {
            "present": bool(environment.get(key)),
            "name_sha256": hashlib.sha256(environment[key].encode()).hexdigest()
            if isinstance(environment.get(key), str) and environment[key]
            else None,
        }
        for key in (
            "ENG_PLATFORM_DEPLOYMENT_EVIDENCE_BUCKET",
            "ENG_PLATFORM_CLOUD_BUILD_EVIDENCE_BUCKET",
        )
    }
    for key in IMAGE_METADATA_KEYS:
        value = environment.get(key)
        result[key] = (
            value
            if isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:_-]*@sha256:[0-9a-f]{64}", value)
            else None
        )
    return result


def summarize_triggers(rows, connected, resource=None):
    """Project only known catalog repositories; unknown triggers expose counts."""
    if (
        not isinstance(rows, list)
        or len(rows) > 201
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise ValueError("bounded trigger array required")
    if not isinstance(connected, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in connected.items()
    ):
        raise ValueError("connected repository mapping must contain strings")
    catalog = policy.SERVICE_REPOSITORIES
    repositories = {
        repo.casefold(): canonical_repository(repo) for repo in catalog.values()
    }
    mapped, active_unmapped, unmapped = [], 0, 0
    for row in rows[:200]:
        if not isinstance(row.get("disabled", False), bool):
            raise ValueError("literal trigger disabled flag required")
        github, event = row.get("github", {}), row.get("repositoryEventConfig", {})
        if not isinstance(github, dict) or not isinstance(event, dict):
            raise ValueError("invalid trigger repository metadata")
        github_name = str(github.get("owner", "")) + "/" + str(github.get("name", ""))
        target = (
            {repositories[github_name.casefold()]}
            if github_name.casefold() in repositories
            else set()
        )
        reference = event.get("repository")
        reference = (
            reference
            if isinstance(reference, str) and re.fullmatch(CONNECTED_PATTERN, reference)
            else None
        )
        connected_targets = (
            {
                canonical_repository(catalog[name])
                for name, value in connected.items()
                if name in catalog and value == reference
            }
            if reference
            else set()
        )
        if (
            connected_targets
            and resource is not None
            and connected_resource(reference) != resource
        ):
            raise ValueError("connected trigger is outside its listed resource")
        if target and connected_targets and target != connected_targets:
            raise ValueError("ambiguous trigger repository")
        target.update(connected_targets)
        disabled = row.get("disabled", False)
        if not target:
            unmapped += 1
            active_unmapped += not disabled
            continue
        record = {"repositories": sorted(target), "disabled": disabled}
        name = row.get("name")
        if isinstance(name, str) and len(name) <= 256:
            record["name"] = name
        if connected_targets:
            record["connected_repository"] = reference
        for source in (github.get("push", {}), event.get("push", {})):
            if not isinstance(source, dict):
                raise ValueError("invalid trigger push metadata")
            for key in ("branch", "tag"):
                value = source.get(key)
                if isinstance(value, str) and len(value) <= 512:
                    if key in record and record[key] != value:
                        raise ValueError("ambiguous trigger filter")
                    record[key] = value
        mapped.append(record)
    return {
        "status": "CERTIFIED" if len(rows) <= 200 else "UNCERTIFIED",
        "mapped_triggers": mapped,
        "mapped_count": len(mapped),
        "active_mapped_count": sum(not row["disabled"] for row in mapped),
        "unmapped_count": unmapped,
        "active_unmapped_count": active_unmapped,
        "limit_reached": len(rows) > 200,
    }


def inspect_triggers(environment):
    """List metadata with existing credentials; never submit or run a build."""
    result = {"resources": [], "private_push_blocked": False}
    try:
        connected = json.loads(
            environment.get(REPOSITORIES_ENV) or "{}",
            object_pairs_hook=policy.byte_json.unique,
        )
        if not isinstance(connected, dict) or any(
            not isinstance(k, str) or not isinstance(v, str)
            for k, v in connected.items()
        ):
            raise ValueError("invalid repository map")
    except (ValueError, TypeError):
        return {
            "resources": [],
            "mapping_status": "UNCERTIFIED",
            "private_push_blocked": True,
        }
    result["mapping_status"] = (
        "UNCERTIFIED"
        if any(
            connected_resource(reference) not in AUDITED_TRIGGER_RESOURCES
            for name, reference in connected.items()
            if name in policy.SERVICE_REPOSITORIES
        )
        else "CERTIFIED"
    )
    result["private_push_blocked"] = result["mapping_status"] != "CERTIFIED"
    for project, region in AUDITED_TRIGGER_RESOURCES:
        entry = {"project": project, "region": region}
        try:
            command = [
                "gcloud",
                "builds",
                "triggers",
                "list",
                "--project",
                project,
                "--region",
                region,
                "--limit=201",
                "--format=" + TRIGGER_FORMAT,
            ]
            read = subprocess.run(command, capture_output=True, check=False, timeout=45)
            if read.returncode:
                match = re.search(rb"\b(401|403|404)\b", read.stderr)
                entry.update(
                    status="UNCERTIFIED",
                    http_status=int(match[1]) if match else None,
                    reason=next(
                        (
                            code
                            for code in (
                                "PERMISSION_DENIED",
                                "UNAUTHENTICATED",
                                "NOT_FOUND",
                                "SERVICE_DISABLED",
                            )
                            if code.encode() in read.stderr
                        ),
                        "unclassified",
                    ),
                )
            else:
                if len(read.stdout) > 1024 * 1024:
                    raise ValueError("trigger metadata response too large")
                entry.update(
                    summarize_triggers(
                        json.loads(
                            read.stdout, object_pairs_hook=policy.byte_json.unique
                        ),
                        connected,
                        (project, region),
                    )
                )
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            entry["status"] = "UNCERTIFIED"
        if (
            entry["status"] != "CERTIFIED"
            or entry.get("active_mapped_count", 0)
            or entry.get("active_unmapped_count", 0)
        ):
            result["private_push_blocked"] = True
        result["resources"].append(entry)
    return result


def configure(candidate_sha):
    if not re.fullmatch(r"[0-9a-f]{64}", candidate_sha):
        raise ValueError("exact inspected candidate SHA256 required")
    existing.BASE_SHA = SOURCE_SHA
    existing.BASE_PATH = "/mnt/eng-platform-catalog/sha256-" + SOURCE_SHA + ".json"
    rollout.CANDIDATE_SHA = candidate_sha
    rollout.OLD_REVISION = SOURCE_REVISION
    rollout.REVISION = "eng-platform-api-cfg-" + candidate_sha[:12]
    rollout.TAG = "catalog-" + candidate_sha[:12]


def inspect(audit=None):
    audit = {} if audit is None else audit
    audit["stage"] = "verify_identity"
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
    audit["stage"] = "read_runtime"
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
    audit["stage"] = "source_identity"
    bucket, key = existing.source_identity(runtime)
    uri = "gs://" + bucket + "/" + key
    audit["stage"] = "source_metadata"
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
    audit["stage"] = "source_bytes"
    raw = existing.read_command(
        "gcloud", "storage", "cat", uri + "#" + SOURCE_GENERATION
    )
    audit["stage"] = "catalog_policy"
    audit["source_hash_matches"] = hashlib.sha256(raw).hexdigest() == SOURCE_SHA
    if audit["source_hash_matches"]:
        rows = policy.byte_json.DECODER.decode(raw.decode())["services"]
        audit["managed_target_shapes"] = [
            {
                "service_name": row["service_name"],
                "management_mode_present": "management_mode" in row,
                "enabled_present": "enabled" in row.get("deployment", {}),
                "enabled_is_true": row.get("deployment", {}).get("enabled", True)
                is True,
            }
            for row in rows
            if row.get("service_name") in policy.SERVICE_REPOSITORIES
        ]
    candidate, proof = policy.prepare(raw)
    # Validation only: no runtime environment or source file is changed.
    os.environ["ENG_PLATFORM_MOCK_MODE"] = "true"
    from eng_platform_api.services.log_catalog import validate_catalog

    audit["stage"] = "catalog_schema"
    validate_catalog(json.loads(raw))
    validate_catalog(json.loads(candidate))
    environment = {
        row["name"]: row.get("value")
        for row in runtime["spec"]["template"]["spec"]["containers"][0].get("env", [])
    }
    audit["stage"] = "inspect_triggers"
    proof.update(
        source_generation=SOURCE_GENERATION,
        existing_account=active,
        existing_runtime_image=existing.BASE_IMAGE,
        bucket_identity_sha256=hashlib.sha256(bucket.encode()).hexdigest(),
        schema_valid=True,
        routing_environment={key: environment.get(key) for key in ROUTING_KEYS},
        routing_environment_changed=False,
        release_runtime_metadata=runtime_metadata(environment),
        cloud_build_triggers=inspect_triggers(environment),
        provider_precedence="explicit release.executor is returned before private policy, deployment executor, circuit or cloud_build_only_services",
        mutation_performed=False,
    )
    audit.update(proof)
    audit["stage"] = "preflight_complete"
    return candidate, audit, bucket, key


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
        audit["stage"] = "configure"
        configure(args.candidate_sha256 if args.mode == "activate" else "0" * 64)
        candidate, audit, bucket, key = inspect(audit)
        if args.mode == "activate":
            if audit["candidate_sha256"] != args.candidate_sha256:
                raise ValueError("candidate differs from inspected approved SHA256")
            publish(candidate, audit, bucket, key)
        else:
            audit["status"] = "read_only_preflight_passed"
    except Exception as error:
        audit["status"] = "blocked"
        audit["error_type"] = type(error).__name__
        audit["error_code"] = SAFE_ERRORS.get(str(error), "unclassified")
        if isinstance(error, (existing.ExistingReadError, rollout.CloudError)):
            audit["http_status"] = error.status
        raise RuntimeError(
            "GitHub defaults catalog operation stopped; inspect bounded audit before retry"
        ) from None
    finally:
        path.write_text(json.dumps(audit, indent=2) + "\n")


if __name__ == "__main__":
    main()
