#!/usr/bin/env python3
"""The small, deterministic release engine used by Actions and Cloud Build.

It never runs test suites, package installation, scanners, semantic-release or
database services.  Input comes exclusively from the signed backend profile.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request

import yaml


ROOT = pathlib.Path("/workspace").resolve()
HOOKS_ROOT = ROOT / "scripts" / "eng-platform-release-hooks"
PROFILE_SPECS = {
    "eng-platform-api": {
        "name": "eng-platform-api",
        "timeout_seconds": 1800,
        "build_args": [],
        "hooks": ["verify_candidate_config"],
        "candidate_update_strategy": "merge",
        "rollback_mode": "traffic",
    },
    "eng-platform-web": {
        "name": "eng-platform-web",
        "timeout_seconds": 1800,
        "build_args": [["APP_VERSION", "{tag}"]],
        "hooks": [],
        "candidate_update_strategy": "merge",
        "rollback_mode": "traffic",
    },
    "communications-ms": {
        "name": "communications-ms",
        "timeout_seconds": 1800,
        "build_args": [],
        "hooks": [],
        "candidate_update_strategy": "overwrite",
        "rollback_mode": "traffic",
    },
    "cgm-bot-api": {
        "name": "cgm-bot-api",
        "timeout_seconds": 1800,
        "build_args": [],
        "hooks": ["ensure_bulk_queue", "deploy_bulk_worker", "validate_smarti"],
        "candidate_update_strategy": "overwrite",
        "rollback_mode": "traffic",
    },
    "cgm-sanplat-web": {
        "name": "cgm-sanplat-web",
        "timeout_seconds": 3600,
        "build_args": [],
        "hooks": ["corporate_window_web", "wait_corporate_activation_web"],
        "candidate_update_strategy": "merge",
        "rollback_mode": "corporate_web",
    },
    "cgm-sanplat-api": {
        "name": "cgm-sanplat-api",
        "timeout_seconds": 3600,
        "build_args": [],
        "hooks": [
            "prepare_corporate_runtimes",
            "deploy_external_jobs",
            "deploy_smarti_prevention",
            "validate_corporate_runtimes",
            "validate_wm_perseo",
            "validate_openapi_inventory",
            "validate_corporate_auth",
            "corporate_window_api",
            "activate_job_engine",
            "wait_corporate_activation_api",
        ],
        "candidate_update_strategy": "merge",
        "rollback_mode": "corporate_api",
    },
}

for _name in (
    "cgm-artemis-api",
    "cgm-artemis-web",
    "cgm-artemis-job-dispatcher",
    "cgm-artemis-job-worker",
    "cgm-artemis-sync-worker",
    "cgm-artemis-clock-sync-worker",
    "cgm-artemis-data-recovery-worker",
    "cgm-artemis-fnd-ip-sync-worker",
    "cgm-artemis-fnd-observation-worker",
    "cgm-artemis-readings-export-worker",
    "cgm-artemis-smarti-prevention-worker",
    "cgm-artemis-wm-sweep-worker",
):
    PROFILE_SPECS[_name] = {
        "name": _name,
        "timeout_seconds": 3600,
        "build_args": [],
        "hooks": [],
        "candidate_update_strategy": "merge",
        "rollback_mode": (
            "job_definition"
            if _name
            in {
                "cgm-artemis-fnd-observation-worker",
                "cgm-artemis-readings-export-worker",
                "cgm-artemis-smarti-prevention-worker",
                "cgm-artemis-wm-sweep-worker",
            }
            else "traffic"
        ),
    }


class AutomaticRollback(RuntimeError):
    """The release failed after promotion and production was restored."""


def env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required engine input: {name}")
    return value


def profile_fingerprint(name: str) -> str:
    payload = json.dumps(PROFILE_SPECS[name], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def verify_profile() -> None:
    name = env("CGM_PROFILE")
    if name not in PROFILE_SPECS:
        raise RuntimeError("unrecognized release profile")
    if profile_fingerprint(name) != env("CGM_PROFILE_SHA256"):
        raise RuntimeError("authorized release profile does not match executor")


def configure_runtime_home() -> None:
    """Use the credential home mounted by Cloud Build, but not in Actions."""
    if os.getenv("BUILD_ID", "").strip():
        os.environ["HOME"] = "/builder/home"


def run(*args: str, cwd: pathlib.Path = ROOT) -> str:
    return subprocess.run(
        args, cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


def emit(stage: str, status: str, **values: str) -> None:
    """Send a compact, monotonic state event. Failure to report is non-fatal."""
    api = os.getenv("CGM_PLATFORM_API_URL", "").rstrip("/")
    if not api or not os.getenv("BUILD_ID", "").strip():
        return
    if status == "running":
        os.environ["CGM_CURRENT_STAGE"] = stage
    sequence = int(os.getenv("CGM_EVENT_SEQUENCE", "0")) + 1
    os.environ["CGM_EVENT_SEQUENCE"] = str(sequence)
    body = {
        "build_id": os.getenv("BUILD_ID", ""),
        "fingerprint": env("CGM_REQUEST_FINGERPRINT"),
        "sequence": sequence,
        "stage": stage,
        "status": status,
        **{key: value for key, value in values.items() if value},
    }
    try:
        token = run("gcloud", "auth", "print-identity-token", f"--audiences={api}")
        request = urllib.request.Request(
            f"{api}/api/internal/deployments/{env('CGM_DEPLOYMENT_ID')}/events",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        urllib.request.urlopen(request, timeout=10).read()
    except Exception as exc:  # Reconciliation is designed for callback loss.
        print(f"event callback unavailable: {exc}", file=sys.stderr)


def assert_source() -> None:
    if not (ROOT / ".git").exists():
        raise RuntimeError("Cloud Build source must be a connected repository checkout")
    # Cloud Build owns the connected-repository checkout with a different UID
    # than the non-root executor. Trust only this exact workspace, never '*'.
    if run("git", "-c", f"safe.directory={ROOT}", "rev-parse", "HEAD") != env(
        "CGM_RELEASE_SHA"
    ):
        raise RuntimeError(
            "connected repository checkout does not match authorized SHA"
        )
    tag = env("CGM_RELEASE_TAG")
    if not re.fullmatch(
        r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
        r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?",
        tag,
    ):
        raise RuntimeError("release tag is not semantic")


def verify_quality() -> None:
    api = env("CGM_PLATFORM_API_URL").rstrip("/")
    endpoint = (
        f"{api}/api/quality/services/{env('CGM_SERVICE')}/commits/"
        f"{env('CGM_RELEASE_SHA')}?for_release=true"
    )
    with urllib.request.urlopen(endpoint, timeout=20) as response:
        report = json.load(response)
    identity = (
        report.get("service_name"),
        report.get("repository"),
        report.get("commit_sha"),
    )
    allowed_owners = {env("CGM_SERVICE")}
    allowed_owners.update(
        value
        for value in os.getenv("CGM_QUALITY_EVIDENCE_SERVICES", "").split(",")
        if value
    )
    if (
        identity[0] not in allowed_owners
        or identity[1]
        not in {
            env("CGM_REPOSITORY"),
            *os.getenv("CGM_REPOSITORY_ALIASES", "").split(","),
        }
        or identity[2] != env("CGM_RELEASE_SHA")
    ):
        raise RuntimeError("quality evidence identity does not match release")
    if (
        report.get("policy_version") != "oss-v2"
        or report.get("quality_gate_status") != "PASSED"
        or not report.get("checks")
        or any(check.get("status") == "FAILED" for check in report["checks"])
    ):
        raise RuntimeError("release requires exact passed oss-v2 evidence")


def image_for_tag() -> str:
    image = env("CGM_IMAGE")
    registry = image.split("/", 1)[0]
    run("gcloud", "auth", "configure-docker", registry, "--quiet")
    try:
        run("docker", "pull", image)
    except subprocess.CalledProcessError:
        existing = False
    else:
        existing = True
    if existing:
        labels = run("docker", "inspect", "--format", "{{json .Config.Labels}}", image)
        data = json.loads(labels or "{}")
        allowed_sources = {env("CGM_REPOSITORY")}
        allowed_sources.update(
            value
            for value in os.getenv("CGM_REPOSITORY_ALIASES", "").split(",")
            if value
        )
        if (
            data.get("org.opencontainers.image.revision") != env("CGM_RELEASE_SHA")
            or data.get("org.opencontainers.image.source") not in allowed_sources
        ):
            raise RuntimeError("existing release image tag has conflicting provenance")
        digest = run(
            "gcloud",
            "artifacts",
            "docker",
            "images",
            "describe",
            image,
            "--format=value(image_summary.digest)",
        )
        if not digest.startswith("sha256:"):
            raise RuntimeError("existing release image has no immutable digest")
        return f"{image.rsplit(':', 1)[0]}@{digest}"
    context = (ROOT / env("CGM_BUILD_CONTEXT")).resolve()
    if ROOT not in context.parents and context != ROOT:
        raise RuntimeError("build context escapes repository")
    dockerfile = (ROOT / os.getenv("CGM_DOCKERFILE_PATH", "Dockerfile")).resolve()
    if (
        ROOT not in dockerfile.parents and dockerfile != ROOT
    ) or not dockerfile.is_file():
        raise RuntimeError("Dockerfile is absent or escapes repository")
    args = [
        "docker",
        "build",
        "--file",
        str(dockerfile),
        "--label",
        f"org.opencontainers.image.revision={env('CGM_RELEASE_SHA')}",
        "--label",
        f"org.opencontainers.image.source={env('CGM_REPOSITORY')}",
        "-t",
        image,
    ]
    if env("CGM_PROFILE") == "eng-platform-web":
        args.extend(["--build-arg", f"APP_VERSION={env('CGM_RELEASE_TAG')}"])
    cache = os.getenv("CGM_CACHE_IMAGE", "").strip()
    if not cache:
        try:
            cache = run(
                "gcloud",
                "run",
                "services",
                "describe",
                env("CGM_SERVICE"),
                "--region",
                env("CGM_REGION"),
                "--project",
                env("CGM_PROJECT_ID"),
                "--format=value(spec.template.spec.containers[0].image)",
            )
        except subprocess.CalledProcessError:
            # A new Artemis runtime has no prior Cloud Run resource yet.
            cache = ""
    if cache:
        subprocess.run(["docker", "pull", cache], check=False, capture_output=True)
        args.extend(["--cache-from", cache])
    run(*args, str(context))
    run("docker", "push", image)  # exactly one push for a new tag
    digest = run(
        "gcloud",
        "artifacts",
        "docker",
        "images",
        "describe",
        image,
        "--format=value(image_summary.digest)",
    )
    if not digest.startswith("sha256:"):
        raise RuntimeError("Artifact Registry did not return an immutable digest")
    return f"{image.split(':', 1)[0]}@{digest}"


def hook(name: str) -> None:
    path = (HOOKS_ROOT / f"{name}.sh").resolve()
    if path.parent != HOOKS_ROOT or not path.is_file():
        raise RuntimeError(f"authorized hook is absent from exact commit: {name}")
    run("bash", str(path))


def _traffic(service: str, region: str, project: str) -> dict[str, int]:
    raw = run(
        "gcloud",
        "run",
        "services",
        "describe",
        service,
        "--region",
        region,
        "--project",
        project,
        "--format=json(status.traffic)",
    )
    parsed = json.loads(raw)
    rows = (
        parsed.get("status", {}).get("traffic", []) if isinstance(parsed, dict) else []
    )
    result = {
        row.get("revisionName", ""): int(row.get("percent", 0))
        for row in rows
        if row.get("revisionName") and int(row.get("percent", 0)) > 0
    }
    if sum(result.values()) != 100:
        raise RuntimeError("production traffic snapshot is not complete")
    return result


def _set_traffic(
    service: str, region: str, project: str, traffic: dict[str, int]
) -> None:
    value = ",".join(
        f"{revision}={percent}" for revision, percent in sorted(traffic.items())
    )
    run(
        "gcloud",
        "run",
        "services",
        "update-traffic",
        service,
        "--to-revisions",
        value,
        "--region",
        region,
        "--project",
        project,
        "--quiet",
    )


def _service_url(service: str, region: str, project: str) -> str:
    return run(
        "gcloud",
        "run",
        "services",
        "describe",
        service,
        "--region",
        region,
        "--project",
        project,
        "--format=value(status.url)",
    )


def _smoke(url: str) -> None:
    target = url.rstrip("/") + "/" + env("CGM_HEALTH_PATH").lstrip("/")
    headers: dict[str, str] = {}
    if os.getenv("CGM_PRIVATE_RUNTIME") == "true":
        audience = _service_url(
            env("CGM_SERVICE"), env("CGM_REGION"), env("CGM_PROJECT_ID")
        )
        token = run("gcloud", "auth", "print-identity-token", f"--audiences={audience}")
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(5):
        try:
            request = urllib.request.Request(target, headers=headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                if 200 <= response.status < 300:
                    return
        except Exception:
            if attempt == 4:
                raise RuntimeError("release smoke failed") from None
            subprocess.run(["sleep", "3"], check=True)


def deploy(image: str) -> dict[str, str]:
    service, region, project = (
        env("CGM_SERVICE"),
        env("CGM_REGION"),
        env("CGM_PROJECT_ID"),
    )
    previous = _traffic(service, region, project)
    emit("deploy-candidate", "running")
    suffix = "ep-" + env("CGM_REQUEST_FINGERPRINT")[:10]
    run(
        "gcloud",
        "run",
        "services",
        "update",
        service,
        "--image",
        image,
        "--region",
        region,
        "--project",
        project,
        "--no-traffic",
        "--revision-suffix",
        suffix,
        "--quiet",
    )
    candidate = run(
        "gcloud",
        "run",
        "services",
        "describe",
        service,
        "--region",
        region,
        "--project",
        project,
        "--format=value(status.latestCreatedRevisionName)",
    )
    candidate_tag = "candidate-" + env("CGM_REQUEST_FINGERPRINT")[:12]
    run(
        "gcloud",
        "run",
        "services",
        "update-traffic",
        service,
        "--update-tags",
        f"{candidate_tag}={candidate}",
        "--region",
        region,
        "--project",
        project,
        "--quiet",
    )
    parsed_traffic = json.loads(
        run(
            "gcloud",
            "run",
            "services",
            "describe",
            service,
            "--region",
            region,
            "--project",
            project,
            "--format=json(status.traffic)",
        )
    )
    traffic_rows = parsed_traffic.get("status", {}).get("traffic", [])
    candidate_url = next(
        (row.get("url", "") for row in traffic_rows if row.get("tag") == candidate_tag),
        "",
    )
    if not candidate_url:
        raise RuntimeError("candidate URL was not created")
    emit(
        "deploy-candidate",
        "succeeded",
        candidate_revision=candidate,
        candidate_url=candidate_url,
        image_digest=image,
    )
    emit("validate-candidate", "running")
    _smoke(candidate_url)
    if env("CGM_PROFILE") == "eng-platform-api":
        run(
            "python3",
            "src/eng_platform_api/verify_candidate_config.py",
            "--project",
            project,
            "--region",
            region,
            "--revision",
            candidate,
        )
    for name in PROFILE_SPECS[env("CGM_PROFILE")]["hooks"]:
        if name != "verify_candidate_config":
            hook(name)
    emit(
        "validate-candidate",
        "succeeded",
        candidate_revision=candidate,
        candidate_url=candidate_url,
    )
    try:
        emit("promote", "running")
        _set_traffic(service, region, project, {candidate: 100})
        emit("promote", "succeeded", production_revision=candidate)
        emit("validate-production", "running")
        production_url = _service_url(service, region, project)
        _smoke(production_url)
        emit(
            "validate-production",
            "succeeded",
            production_revision=candidate,
            production_url=production_url,
        )
        return {
            "candidate_revision": candidate,
            "candidate_url": candidate_url,
            "production_revision": candidate,
            "production_url": production_url,
            "image_digest": image,
        }
    except Exception:
        _set_traffic(service, region, project, previous)
        restored = max(previous, key=previous.get)
        emit("rollback", "succeeded", production_revision=restored)
        raise AutomaticRollback("production smoke failed; traffic was restored")


def rollback() -> dict[str, str]:
    if os.getenv("CGM_RUNTIME_KIND") == "cloud_run_job":
        return rollback_job()
    target = env("CGM_TARGET_REVISION")
    emit("rollback", "running")
    run(
        "gcloud",
        "run",
        "services",
        "update-traffic",
        env("CGM_SERVICE"),
        "--to-revisions",
        f"{target}=100",
        "--region",
        env("CGM_REGION"),
        "--project",
        env("CGM_PROJECT_ID"),
        "--quiet",
    )
    emit("rollback", "succeeded", production_revision=target)
    return {
        "production_revision": target,
        "production_url": _service_url(
            env("CGM_SERVICE"), env("CGM_REGION"), env("CGM_PROJECT_ID")
        ),
    }


def _job_definition() -> str:
    """Export a restorable definition, not a status-bearing API response."""
    return (
        run(
            "gcloud",
            "run",
            "jobs",
            "describe",
            env("CGM_SERVICE"),
            "--region",
            env("CGM_REGION"),
            "--project",
            env("CGM_PROJECT_ID"),
            "--format=export",
        )
        + "\n"
    )


def _job_operational_spec(definition: str) -> dict:
    payload = yaml.safe_load(definition)
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "Job"
        or payload.get("metadata", {}).get("name") != env("CGM_SERVICE")
    ):
        raise RuntimeError("Job snapshot does not match authorized resource")
    spec = payload.get("spec")
    if not isinstance(spec, dict):
        raise RuntimeError("Job snapshot lacks a specification")
    metadata = spec.get("template", {}).get("metadata", {})
    if isinstance(metadata, dict):
        for key in ("annotations", "labels"):
            values = metadata.get(key)
            if isinstance(values, dict):
                for volatile in (
                    "run.googleapis.com/client-name",
                    "run.googleapis.com/client-version",
                    "client.knative.dev/nonce",
                ):
                    values.pop(volatile, None)
    return spec


def _job_image() -> str:
    payload = json.loads(
        run(
            "gcloud",
            "run",
            "jobs",
            "describe",
            env("CGM_SERVICE"),
            "--region",
            env("CGM_REGION"),
            "--project",
            env("CGM_PROJECT_ID"),
            "--format=json",
        )
    )
    containers = (
        payload.get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("template", {})
        .get("spec", {})
        .get("containers", [])
    )
    if len(containers) != 1 or not containers[0].get("image"):
        raise RuntimeError("Job must have exactly one configured container")
    return str(containers[0]["image"])


def _job_snapshot_uri(revision: str) -> str:
    if not re.fullmatch(r"job-[0-9a-f]{64}", revision):
        raise RuntimeError("invalid Job snapshot fingerprint")
    bucket = env("CGM_EVIDENCE_BUCKET")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,222}", bucket):
        raise RuntimeError("invalid evidence bucket")
    return f"gs://{bucket}/job-definitions/{env('CGM_SERVICE')}/{revision}.yaml"


def _replace_job(definition: str) -> None:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", encoding="utf-8"
    ) as file:
        file.write(definition)
        file.flush()
        run(
            "gcloud",
            "run",
            "jobs",
            "replace",
            file.name,
            "--region",
            env("CGM_REGION"),
            "--project",
            env("CGM_PROJECT_ID"),
            "--quiet",
        )


def _save_job_definition(definition: str) -> str:
    revision = "job-" + hashlib.sha256(definition.encode()).hexdigest()
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", encoding="utf-8"
    ) as file:
        file.write(definition)
        file.flush()
        run("gcloud", "storage", "cp", file.name, _job_snapshot_uri(revision))
    return revision


def deploy_job(image: str) -> dict[str, str]:
    """Update one pre-provisioned Job without running business work."""
    if "@sha256:" not in image:
        raise RuntimeError("Job deployment requires an immutable image")
    before = _job_definition()
    _job_operational_spec(before)
    _job_image()  # Refuse a malformed/multi-container Job before any mutation.
    _save_job_definition(before)  # Durable recovery before touching the Job.
    emit("deploy-candidate", "running")
    emit("deploy-candidate", "succeeded", candidate_revision=image, image_digest=image)
    emit("validate-candidate", "running")
    emit("validate-candidate", "succeeded", candidate_revision=image)
    emit("promote", "running")
    try:
        run(
            "gcloud",
            "run",
            "jobs",
            "update",
            env("CGM_SERVICE"),
            "--image",
            image,
            "--region",
            env("CGM_REGION"),
            "--project",
            env("CGM_PROJECT_ID"),
            "--quiet",
        )
        if _job_image() != image:
            raise RuntimeError("updated Job image does not match authorized digest")
        definition = _job_definition()
        revision = _save_job_definition(definition)
        emit("promote", "succeeded", production_revision=revision)
        emit("validate-production", "running")
        emit("validate-production", "succeeded", production_revision=revision)
        return {
            "candidate_revision": image,
            "production_revision": revision,
            "image_digest": image,
        }
    except Exception:
        try:
            _replace_job(before)
        except Exception as restore_exc:
            emit("rollback", "failed", error="Job definition restoration failed")
            raise RuntimeError(
                "Job update failed and prior definition could not be restored"
            ) from restore_exc
        emit("rollback", "succeeded")
        raise AutomaticRollback("Job update failed; prior definition was restored")


def rollback_job() -> dict[str, str]:
    """Restore a previously deployed Job definition after checksum validation."""
    target_revision = env("CGM_TARGET_REVISION")
    if not re.fullmatch(r"job-[0-9a-f]{64}", target_revision):
        raise RuntimeError("invalid target Job definition fingerprint")
    before = _job_definition()
    emit("rollback", "running")
    with tempfile.NamedTemporaryFile(
        mode="w+", suffix=".yaml", encoding="utf-8"
    ) as file:
        run("gcloud", "storage", "cp", _job_snapshot_uri(target_revision), file.name)
        file.seek(0)
        target = file.read()
    if "job-" + hashlib.sha256(target.encode()).hexdigest() != target_revision:
        raise RuntimeError("target Job definition fingerprint mismatch")
    expected_spec = _job_operational_spec(target)
    try:
        _replace_job(target)
        if _job_operational_spec(_job_definition()) != expected_spec:
            raise RuntimeError("restored Job definition differs from target")
    except Exception:
        _replace_job(before)
        raise
    emit("rollback", "succeeded", production_revision=target_revision)
    return {"production_revision": target_revision, "image_digest": _job_image()}


def write_summary(values: dict[str, str]) -> None:
    summary = {
        "deployment_id": env("CGM_DEPLOYMENT_ID"),
        "fingerprint": env("CGM_REQUEST_FINGERPRINT"),
        "service": env("CGM_SERVICE"),
        "sha": env("CGM_RELEASE_SHA"),
        "tag": env("CGM_RELEASE_TAG"),
        **values,
    }
    # The workspace is bind-mounted by GitHub Actions and retained by Cloud
    # Build, so the caller can publish the immutable revision and URL.
    path = ROOT / ".eng-platform-release-result.json"
    path.write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")
    bucket = os.getenv("CGM_EVIDENCE_BUCKET", "").strip()
    if bucket:
        destination = (
            f"gs://{bucket}/deployment-summaries/{env('CGM_DEPLOYMENT_ID')}.json"
        )
        run("gcloud", "storage", "cp", str(path), destination)


def main() -> None:
    configure_runtime_home()
    verify_profile()
    assert_source()
    emit("verify-release", "running")
    if env("CGM_OPERATION") != "rollback":
        verify_quality()
    emit("verify-release", "succeeded")
    if env("CGM_OPERATION") == "rollback":
        write_summary(rollback())
        return
    emit("build", "running")
    image = image_for_tag()
    emit("build", "succeeded", image_digest=image)
    if os.getenv("CGM_RUNTIME_KIND") == "cloud_run_job":
        write_summary(deploy_job(image))
    else:
        write_summary(deploy(image))


if __name__ == "__main__":
    try:
        main()
    except AutomaticRollback:
        raise
    except Exception as exc:
        emit(os.getenv("CGM_CURRENT_STAGE", "verify-release"), "failed", error=str(exc))
        raise
