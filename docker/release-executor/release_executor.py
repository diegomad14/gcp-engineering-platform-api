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
import time
import urllib.error
import urllib.parse
import urllib.request

import yaml


ROOT = pathlib.Path("/workspace").resolve()
HOOKS_ROOT = ROOT / "scripts" / "eng-platform-release-hooks"
PROFILE_SPECS = {
    "eng-platform-api": {
        "name": "eng-platform-api",
        "timeout_seconds": 1800,
        "build_args": [],
        "candidate_env_vars": [],
        "pre_candidate_hooks": [],
        "candidate_hooks": ["verify_candidate_config"],
        "pre_promote_hooks": [],
        "post_promote_hooks": [],
        "recovery_hook": "",
        "rollback_hook": "",
        "candidate_update_strategy": "merge",
        "rollback_mode": "traffic",
    },
    "eng-platform-web": {
        "name": "eng-platform-web",
        "timeout_seconds": 1800,
        "build_args": [["APP_VERSION", "{tag}"]],
        "candidate_env_vars": [],
        "pre_candidate_hooks": [],
        "candidate_hooks": [],
        "pre_promote_hooks": [],
        "post_promote_hooks": [],
        "recovery_hook": "",
        "rollback_hook": "",
        "candidate_update_strategy": "merge",
        "rollback_mode": "traffic",
    },
    "communications-ms": {
        "name": "communications-ms",
        "timeout_seconds": 1800,
        "build_args": [],
        "candidate_env_vars": [],
        "pre_candidate_hooks": [],
        "candidate_hooks": [],
        "pre_promote_hooks": [],
        "post_promote_hooks": [],
        "recovery_hook": "",
        "rollback_hook": "",
        "candidate_update_strategy": "overwrite",
        "rollback_mode": "traffic",
    },
    "cgm-bot-api": {
        "name": "cgm-bot-api",
        "timeout_seconds": 1800,
        "build_args": [],
        "candidate_env_vars": [],
        "pre_candidate_hooks": ["ensure_bulk_queue", "deploy_bulk_worker"],
        "candidate_hooks": ["validate_smarti"],
        "pre_promote_hooks": [],
        "post_promote_hooks": ["promote_bulk_worker"],
        "recovery_hook": "recover_bulk_worker",
        "rollback_hook": "rollback_bulk_worker",
        "candidate_update_strategy": "overwrite",
        "rollback_mode": "traffic",
    },
    "cgm-sanplat-web": {
        "name": "cgm-sanplat-web",
        "timeout_seconds": 3600,
        "build_args": [],
        "candidate_env_vars": [],
        "pre_candidate_hooks": [],
        "candidate_hooks": [],
        "pre_promote_hooks": ["corporate_window_web"],
        "post_promote_hooks": ["wait_corporate_activation_web"],
        "recovery_hook": "recover_corporate_web",
        "rollback_hook": "rollback_corporate_web",
        "candidate_update_strategy": "merge",
        "rollback_mode": "corporate_web",
    },
    "cgm-sanplat-api": {
        "name": "cgm-sanplat-api",
        "timeout_seconds": 3600,
        "build_args": [],
        "candidate_env_vars": [["APP_RELEASE_SHA", "{sha}"]],
        "pre_candidate_hooks": ["prepare_corporate_runtimes"],
        "candidate_hooks": [
            "validate_openapi_inventory",
            "validate_corporate_auth",
        ],
        "pre_promote_hooks": ["corporate_window_api"],
        "post_promote_hooks": ["wait_corporate_activation_api"],
        "recovery_hook": "recover_corporate_api",
        "rollback_hook": "rollback_corporate_api",
        "candidate_update_strategy": "merge",
        "rollback_mode": "corporate_api",
    },
}

# Mirrored from central release_profiles.py; parity is tested.
PROFILE_SPECS.update(
    {
        "cgm-artemis-api": {
            "name": "cgm-artemis-api",
            "timeout_seconds": 3600,
            "build_args": [],
            "candidate_env_vars": [
                ["APP_RELEASE_SHA", "{sha}"],
                ["APP_RELEASE_SCOPE", "runtime-v1"],
                ["APP_RELEASE_RESOURCE", "cgm-artemis-api"],
                ["APP_BACKGROUND_TASKS_ENABLED", "false"],
            ],
            "pre_candidate_hooks": [],
            "candidate_hooks": [],
            "pre_promote_hooks": [],
            "post_promote_hooks": [],
            "recovery_hook": "",
            "rollback_hook": "",
            "candidate_update_strategy": "merge",
            "rollback_mode": "traffic",
        },
        "cgm-artemis-web": {
            "name": "cgm-artemis-web",
            "timeout_seconds": 3600,
            "build_args": [["APP_VERSION", "{tag}"]],
            "candidate_env_vars": [],
            "pre_candidate_hooks": [],
            "candidate_hooks": [],
            "pre_promote_hooks": [],
            "post_promote_hooks": [],
            "recovery_hook": "",
            "rollback_hook": "",
            "candidate_update_strategy": "merge",
            "rollback_mode": "traffic",
        },
        "cgm-artemis-job-dispatcher": {
            "name": "cgm-artemis-job-dispatcher",
            "timeout_seconds": 3600,
            "build_args": [],
            "candidate_env_vars": [
                ["APP_RELEASE_SHA", "{sha}"],
                ["APP_RELEASE_SCOPE", "runtime-v1"],
                ["APP_RELEASE_RESOURCE", "cgm-artemis-job-dispatcher"],
                ["JOB_WORKER_PREFIX", "cgm-artemis"],
                [
                    "JOB_TASK_OIDC_SERVICE_ACCOUNT",
                    "artemis-tasks-invoker@cgm-assistant-prod.iam.gserviceaccount.com",
                ],
                ["ARTEMIS_SYNC_QUEUE", "cgm-artemis-sync"],
                ["ARTEMIS_SYNC_WORKER_URL", "{service_uri:cgm-artemis-sync-worker}"],
                ["ARTEMIS_CLOCK_SYNC_QUEUE", "cgm-artemis-clock-sync"],
                [
                    "ARTEMIS_CLOCK_SYNC_WORKER_URL",
                    "{service_uri:cgm-artemis-clock-sync-worker}",
                ],
                ["ARTEMIS_DATA_RECOVERY_QUEUE", "cgm-artemis-data-recovery"],
                [
                    "ARTEMIS_DATA_RECOVERY_WORKER_URL",
                    "{service_uri:cgm-artemis-data-recovery-worker}",
                ],
                ["ARTEMIS_FND_IP_SYNC_QUEUE", "cgm-artemis-fnd-ip-sync"],
                [
                    "ARTEMIS_FND_IP_SYNC_WORKER_URL",
                    "{service_uri:cgm-artemis-fnd-ip-sync-worker}",
                ],
            ],
            "pre_candidate_hooks": [],
            "candidate_hooks": [],
            "pre_promote_hooks": [],
            "post_promote_hooks": [],
            "recovery_hook": "",
            "rollback_hook": "",
            "candidate_update_strategy": "merge",
            "rollback_mode": "traffic",
        },
        "cgm-artemis-job-worker": {
            "name": "cgm-artemis-job-worker",
            "timeout_seconds": 3600,
            "build_args": [],
            "candidate_env_vars": [
                ["APP_RELEASE_SHA", "{sha}"],
                ["APP_RELEASE_SCOPE", "runtime-v1"],
                ["APP_RELEASE_RESOURCE", "cgm-artemis-job-worker"],
            ],
            "pre_candidate_hooks": [],
            "candidate_hooks": [],
            "pre_promote_hooks": [],
            "post_promote_hooks": [],
            "recovery_hook": "",
            "rollback_hook": "",
            "candidate_update_strategy": "merge",
            "rollback_mode": "traffic",
        },
        "cgm-artemis-sync-worker": {
            "name": "cgm-artemis-sync-worker",
            "timeout_seconds": 3600,
            "build_args": [],
            "candidate_env_vars": [
                ["APP_RELEASE_SHA", "{sha}"],
                ["APP_RELEASE_SCOPE", "runtime-v1"],
                ["APP_RELEASE_RESOURCE", "cgm-artemis-sync-worker"],
                ["ARTEMIS_WORKER_TYPE", "sync"],
            ],
            "pre_candidate_hooks": [],
            "candidate_hooks": [],
            "pre_promote_hooks": [],
            "post_promote_hooks": [],
            "recovery_hook": "",
            "rollback_hook": "",
            "candidate_update_strategy": "merge",
            "rollback_mode": "traffic",
        },
        "cgm-artemis-clock-sync-worker": {
            "name": "cgm-artemis-clock-sync-worker",
            "timeout_seconds": 3600,
            "build_args": [],
            "candidate_env_vars": [
                ["APP_RELEASE_SHA", "{sha}"],
                ["APP_RELEASE_SCOPE", "runtime-v1"],
                ["APP_RELEASE_RESOURCE", "cgm-artemis-clock-sync-worker"],
                ["ARTEMIS_WORKER_TYPE", "clock-sync"],
            ],
            "pre_candidate_hooks": [],
            "candidate_hooks": [],
            "pre_promote_hooks": [],
            "post_promote_hooks": [],
            "recovery_hook": "",
            "rollback_hook": "",
            "candidate_update_strategy": "merge",
            "rollback_mode": "traffic",
        },
        "cgm-artemis-data-recovery-worker": {
            "name": "cgm-artemis-data-recovery-worker",
            "timeout_seconds": 3600,
            "build_args": [],
            "candidate_env_vars": [
                ["APP_RELEASE_SHA", "{sha}"],
                ["APP_RELEASE_SCOPE", "runtime-v1"],
                ["APP_RELEASE_RESOURCE", "cgm-artemis-data-recovery-worker"],
                ["ARTEMIS_WORKER_TYPE", "data-recovery"],
            ],
            "pre_candidate_hooks": [],
            "candidate_hooks": [],
            "pre_promote_hooks": [],
            "post_promote_hooks": [],
            "recovery_hook": "",
            "rollback_hook": "",
            "candidate_update_strategy": "merge",
            "rollback_mode": "traffic",
        },
        "cgm-artemis-fnd-ip-sync-worker": {
            "name": "cgm-artemis-fnd-ip-sync-worker",
            "timeout_seconds": 3600,
            "build_args": [],
            "candidate_env_vars": [
                ["APP_RELEASE_SHA", "{sha}"],
                ["APP_RELEASE_SCOPE", "runtime-v1"],
                ["APP_RELEASE_RESOURCE", "cgm-artemis-fnd-ip-sync-worker"],
                ["ARTEMIS_WORKER_TYPE", "fnd-ip-sync"],
            ],
            "pre_candidate_hooks": [],
            "candidate_hooks": [],
            "pre_promote_hooks": [],
            "post_promote_hooks": [],
            "recovery_hook": "",
            "rollback_hook": "",
            "candidate_update_strategy": "merge",
            "rollback_mode": "traffic",
        },
        "cgm-artemis-mcp-worker": {
            "name": "cgm-artemis-mcp-worker",
            "timeout_seconds": 3600,
            "build_args": [],
            "candidate_env_vars": [
                ["APP_RELEASE_SHA", "{sha}"],
                ["APP_RELEASE_SCOPE", "runtime-v1"],
                ["APP_RELEASE_RESOURCE", "cgm-artemis-mcp-worker"],
                ["ARTEMIS_WORKER_TYPE", "mcp-parameterization"],
            ],
            "pre_candidate_hooks": [],
            "candidate_hooks": [],
            "pre_promote_hooks": [],
            "post_promote_hooks": [],
            "recovery_hook": "",
            "rollback_hook": "",
            "candidate_update_strategy": "merge",
            "rollback_mode": "traffic",
        },
        "cgm-artemis-fnd-observation-worker": {
            "name": "cgm-artemis-fnd-observation-worker",
            "timeout_seconds": 3600,
            "build_args": [],
            "candidate_env_vars": [
                ["APP_RELEASE_SHA", "{sha}"],
                ["APP_RELEASE_SCOPE", "runtime-v1"],
                ["APP_RELEASE_RESOURCE", "cgm-artemis-fnd-observation-worker"],
                ["APP_RUNTIME_CHECK_ONLY", "true"],
                ["ARTEMIS_WORKER_TYPE", "fnd-observation"],
            ],
            "pre_candidate_hooks": [],
            "candidate_hooks": [],
            "pre_promote_hooks": [],
            "post_promote_hooks": [],
            "recovery_hook": "",
            "rollback_hook": "",
            "candidate_update_strategy": "merge",
            "rollback_mode": "job_definition",
        },
        "cgm-artemis-readings-export-worker": {
            "name": "cgm-artemis-readings-export-worker",
            "timeout_seconds": 3600,
            "build_args": [],
            "candidate_env_vars": [
                ["APP_RELEASE_SHA", "{sha}"],
                ["APP_RELEASE_SCOPE", "runtime-v1"],
                ["APP_RELEASE_RESOURCE", "cgm-artemis-readings-export-worker"],
                ["APP_RUNTIME_CHECK_ONLY", "true"],
                ["ARTEMIS_WORKER_TYPE", "readings-export"],
            ],
            "pre_candidate_hooks": [],
            "candidate_hooks": [],
            "pre_promote_hooks": [],
            "post_promote_hooks": [],
            "recovery_hook": "",
            "rollback_hook": "",
            "candidate_update_strategy": "merge",
            "rollback_mode": "job_definition",
        },
        "cgm-artemis-smarti-prevention-worker": {
            "name": "cgm-artemis-smarti-prevention-worker",
            "timeout_seconds": 3600,
            "build_args": [],
            "candidate_env_vars": [
                ["APP_RELEASE_SHA", "{sha}"],
                ["APP_RELEASE_SCOPE", "runtime-v1"],
                ["APP_RELEASE_RESOURCE", "cgm-artemis-smarti-prevention-worker"],
                ["APP_RUNTIME_CHECK_ONLY", "true"],
                ["ARTEMIS_WORKER_TYPE", "smarti-prevention"],
            ],
            "pre_candidate_hooks": [],
            "candidate_hooks": [],
            "pre_promote_hooks": [],
            "post_promote_hooks": [],
            "recovery_hook": "",
            "rollback_hook": "",
            "candidate_update_strategy": "merge",
            "rollback_mode": "job_definition",
        },
        "cgm-artemis-wm-sweep-worker": {
            "name": "cgm-artemis-wm-sweep-worker",
            "timeout_seconds": 3600,
            "build_args": [],
            "candidate_env_vars": [
                ["APP_RELEASE_SHA", "{sha}"],
                ["APP_RELEASE_SCOPE", "runtime-v1"],
                ["APP_RELEASE_RESOURCE", "cgm-artemis-wm-sweep-worker"],
                ["APP_RUNTIME_CHECK_ONLY", "true"],
                ["ARTEMIS_WORKER_TYPE", "wm-sweep"],
            ],
            "pre_candidate_hooks": [],
            "candidate_hooks": [],
            "pre_promote_hooks": [],
            "post_promote_hooks": [],
            "recovery_hook": "",
            "rollback_hook": "",
            "candidate_update_strategy": "merge",
            "rollback_mode": "job_definition",
        },
    }
)


class AutomaticRollback(RuntimeError):
    """The release failed after promotion and production was restored."""


def env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required engine input: {name}")
    return value


def profile_fingerprint(name: str) -> str:
    value = PROFILE_SPECS[name]
    if name in {"eng-platform-api", "eng-platform-web", "communications-ms"}:
        value = {
            "name": value["name"],
            "timeout_seconds": value["timeout_seconds"],
            "build_args": value["build_args"],
            "hooks": value["candidate_hooks"],
            "candidate_update_strategy": value["candidate_update_strategy"],
            "rollback_mode": value["rollback_mode"],
        }
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
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


class CommandFailure(subprocess.CalledProcessError):
    def __str__(self) -> str:
        command = sanitized_error(" ".join(str(value) for value in self.cmd))
        detail = sanitized_error(self.stderr or self.stdout or "no diagnostic output")
        return f"command failed (exit {self.returncode}): {command}: {detail}"


def run(*args: str, cwd: pathlib.Path = ROOT) -> str:
    try:
        return subprocess.run(
            args, cwd=cwd, check=True, text=True, capture_output=True
        ).stdout.strip()
    except subprocess.CalledProcessError as exc:
        raise CommandFailure(
            exc.returncode, exc.cmd, output=exc.stdout, stderr=exc.stderr
        ) from None


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

    if independent_runtime():
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", encoding="utf-8"
        ) as file:
            json.dump(report, file)
            file.flush()
            run(
                "gcloud",
                "storage",
                "cp",
                file.name,
                f"gs://{env('CGM_EVIDENCE_BUCKET')}/release-evidence/{env('CGM_DEPLOYMENT_ID')}.json",
            )


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
    if env("CGM_PROFILE") in {"eng-platform-web", "cgm-artemis-web"}:
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
    try:
        run("bash", str(path))
    except subprocess.CalledProcessError as exc:
        detail = sanitized_error(exc.stderr or exc.stdout or "no diagnostic output")
        raise RuntimeError(
            f"hook {name} failed (exit {exc.returncode}): {detail}"
        ) from None


def sanitized_error(message: str) -> str:
    for key, value in os.environ.items():
        if len(value) >= 6 and any(
            part in key.upper()
            for part in ("TOKEN", "SECRET", "PASSWORD", "DATABASE_URL")
        ):
            message = message.replace(value, "[redacted]")
    message = re.sub(r"(?i)(bearer\s+)[^\s\"']+", r"\1[redacted]", message)
    message = re.sub(
        r"(?i)((?:password|secret|token)\s*[=:]\s*)[^\s,;]+", r"\1[redacted]", message
    )
    message = re.sub(
        r"([a-z][a-z0-9+.-]*://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", message
    )
    return message[-2000:]


def verify_hooks() -> None:
    """Reject an incomplete tagged source before building or changing resources."""
    spec = PROFILE_SPECS[env("CGM_PROFILE")]
    names = [
        *spec["pre_candidate_hooks"],
        *spec["candidate_hooks"],
        *spec["pre_promote_hooks"],
        *spec["post_promote_hooks"],
        spec["recovery_hook"],
        spec["rollback_hook"],
    ]
    for name in filter(None, names):
        if name == "verify_candidate_config":
            continue
        path = (HOOKS_ROOT / f"{name}.sh").resolve()
        if path.parent != HOOKS_ROOT or not path.is_file():
            raise RuntimeError(f"authorized hook is absent from exact commit: {name}")


def run_hooks(phase: str) -> None:
    for name in PROFILE_SPECS[env("CGM_PROFILE")][phase]:
        if name == "verify_candidate_config":
            run(
                "python3",
                "src/eng_platform_api/verify_candidate_config.py",
                "--project",
                env("CGM_PROJECT_ID"),
                "--region",
                env("CGM_REGION"),
                "--revision",
                env("CGM_CANDIDATE_REVISION"),
            )
        else:
            hook(name)


def candidate_env_args() -> list[str]:
    """Render only server-owned release variables, never caller-supplied names."""
    profile_name = env("CGM_PROFILE")
    rows = PROFILE_SPECS[profile_name]["candidate_env_vars"]
    if not rows:
        return []
    values = []
    for name, template in rows:
        if template == "{sha}" and name == "APP_RELEASE_SHA":
            value = env("CGM_RELEASE_SHA")
        elif name == "ARTEMIS_WORKER_TYPE":
            expected = {
                "cgm-artemis-sync-worker": "sync",
                "cgm-artemis-clock-sync-worker": "clock-sync",
                "cgm-artemis-data-recovery-worker": "data-recovery",
                "cgm-artemis-fnd-ip-sync-worker": "fnd-ip-sync",
                "cgm-artemis-mcp-worker": "mcp-parameterization",
                "cgm-artemis-fnd-observation-worker": "fnd-observation",
                "cgm-artemis-readings-export-worker": "readings-export",
                "cgm-artemis-smarti-prevention-worker": "smarti-prevention",
                "cgm-artemis-wm-sweep-worker": "wm-sweep",
            }.get(profile_name)
            if template != expected:
                raise RuntimeError("task worker type does not match release profile")
            value = template
        elif profile_name.startswith("cgm-artemis-") and (
            (name == "APP_RELEASE_SCOPE" and template == "runtime-v1")
            or (name == "APP_RELEASE_RESOURCE" and template == profile_name)
            or (name == "APP_RUNTIME_CHECK_ONLY" and template == "true")
        ):
            value = template
        elif template.startswith("{service_uri:") and template.endswith("}"):
            target = template[len("{service_uri:") : -1]
            allowed_routes = {
                "cgm-artemis-sync-worker": "ARTEMIS_SYNC_WORKER_URL",
                "cgm-artemis-clock-sync-worker": "ARTEMIS_CLOCK_SYNC_WORKER_URL",
                "cgm-artemis-data-recovery-worker": "ARTEMIS_DATA_RECOVERY_WORKER_URL",
                "cgm-artemis-fnd-ip-sync-worker": "ARTEMIS_FND_IP_SYNC_WORKER_URL",
            }
            if (
                profile_name != "cgm-artemis-job-dispatcher"
                or allowed_routes.get(target) != name
            ):
                raise RuntimeError("service URL is not allowed by dispatcher profile")
            value = _service_url(target, env("CGM_REGION"), env("CGM_PROJECT_ID"))
            host = urllib.parse.urlsplit(value)
            expected_host = re.compile(
                rf"{re.escape(target)}-(?:[0-9]{{12}}\.{re.escape(env('CGM_REGION'))}"
                r"|[a-z0-9]{10}-uc)\.a\.run\.app"
            )
            if (
                host.scheme != "https"
                or not expected_host.fullmatch(host.hostname or "")
                or host.path not in {"", "/"}
                or host.query
                or host.fragment
                or host.username
                or host.port is not None
            ):
                raise RuntimeError("Cloud Run returned an invalid Artemis worker URL")
        elif (
            profile_name == "cgm-artemis-api"
            and name == "APP_BACKGROUND_TASKS_ENABLED"
            and template == "false"
        ):
            value = template
        elif profile_name == "cgm-artemis-job-dispatcher" and (
            (name == "JOB_WORKER_PREFIX" and template == "cgm-artemis")
            or (
                name == "JOB_TASK_OIDC_SERVICE_ACCOUNT"
                and template
                == "artemis-tasks-invoker@cgm-assistant-prod.iam.gserviceaccount.com"
            )
            or (
                name
                in {
                    "ARTEMIS_SYNC_QUEUE",
                    "ARTEMIS_CLOCK_SYNC_QUEUE",
                    "ARTEMIS_DATA_RECOVERY_QUEUE",
                    "ARTEMIS_FND_IP_SYNC_QUEUE",
                }
                and template.startswith("cgm-artemis-")
            )
        ):
            value = template
        else:
            raise RuntimeError("unsupported candidate environment template")
        values.append(f"{name}={value}")
    return ["--update-env-vars", ",".join(values)]


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


def _deploy_candidate(
    image: str, service: str, region: str, project: str
) -> tuple[str, str]:
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
        "--update-labels",
        f"commit-sha={env('CGM_RELEASE_SHA')}",
        *candidate_env_args(),
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
    # Cloud Run limits the combined service name and traffic tag to 46 chars.
    candidate_tag = "c-" + env("CGM_REQUEST_FINGERPRINT")[:12]
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
    return candidate, candidate_url


def independent_runtime() -> bool:
    return (
        env("CGM_SERVICE").startswith("cgm-artemis-")
        and env("CGM_SERVICE") != "cgm-artemis-web"
    )


def runtime_grant(action: str, sha: str) -> None:
    """The executor invokes a bounded management job; it has no SQL credentials."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RuntimeError("runtime grant requires an exact commit")
    operation = f"{env('CGM_DEPLOYMENT_ID')}:{action}:{sha}"
    evidence = f"gs://{env('CGM_EVIDENCE_BUCKET')}/release-evidence/{env('CGM_DEPLOYMENT_ID')}.json"
    run(
        "gcloud",
        "run",
        "jobs",
        "execute",
        "cgm-artemis-corporate-activate",
        "--region",
        env("CGM_REGION"),
        "--project",
        env("CGM_PROJECT_ID"),
        "--args="
        + ",".join(
            [
                "-m",
                "cgm_sanplat_param.security.runtime_rollout",
                action,
                "--resource",
                env("CGM_SERVICE"),
                "--sha",
                sha,
                "--event-id",
                operation,
                "--evidence",
                evidence,
            ]
        ),
        "--wait",
        "--quiet",
    )


def revision_runtime(revision: str) -> dict[str, str]:
    payload = json.loads(
        run(
            "gcloud",
            "run",
            "revisions",
            "describe",
            revision,
            "--region",
            env("CGM_REGION"),
            "--project",
            env("CGM_PROJECT_ID"),
            "--format=json",
        )
    )
    return {
        item["name"]: item.get("value", "")
        for item in payload["spec"]["containers"][0].get("env", [])
    }


def _runtime_ready_once(url: str, *, candidate: bool = False, sha: str = "") -> None:
    """Accept only the expected identity, protocol, commit and activation result."""
    expected_sha = sha or env("CGM_RELEASE_SHA")
    headers = {}
    if os.getenv("CGM_PRIVATE_RUNTIME") == "true":
        audience = _service_url(
            env("CGM_SERVICE"), env("CGM_REGION"), env("CGM_PROJECT_ID")
        )
        headers["Authorization"] = "Bearer " + run(
            "gcloud", "auth", "print-identity-token", f"--audiences={audience}"
        )
    request = urllib.request.Request(url.rstrip("/") + "/ready", headers=headers)
    try:
        response = urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code != 503 or not candidate:
            raise RuntimeError(f"runtime readiness HTTP {exc.code}") from None
        response = exc
    with response:
        payload = json.load(response)
    payload = payload.get("detail", payload)
    if (
        payload.get("resource") != env("CGM_SERVICE")
        or payload.get("release_sha") != expected_sha
        or payload.get("scope") != "runtime-v1"
    ):
        raise RuntimeError("runtime readiness identity or activation protocol mismatch")
    if payload.get("status") == "ready" and payload.get("reason") == "active":
        return
    if candidate and payload.get("reason") == "release_not_activated":
        return
    raise RuntimeError(
        "runtime readiness blocked: " + str(payload.get("reason", "invalid_response"))
    )


def runtime_ready(url: str, *, candidate: bool = False, sha: str = "") -> None:
    for attempt in range(6):
        try:
            _runtime_ready_once(url, candidate=candidate, sha=sha)
            return
        except RuntimeError:
            if candidate or attempt == 5:
                raise
            # Activation gates cache for five seconds; traffic also propagates.
            time.sleep(1)


def verify_artemis_web() -> None:
    if env("CGM_SERVICE") != "cgm-artemis-api":
        return
    expected = env("CGM_PAIRED_WEB_SHA")
    traffic = _traffic("cgm-artemis-web", env("CGM_REGION"), env("CGM_PROJECT_ID"))
    for revision, percent in traffic.items():
        if not percent:
            continue
        sha = run(
            "gcloud",
            "run",
            "revisions",
            "describe",
            revision,
            "--region",
            env("CGM_REGION"),
            "--project",
            env("CGM_PROJECT_ID"),
            "--format=value(metadata.labels.commit-sha)",
        )
        if sha != expected:
            raise RuntimeError("API paired web commit does not match serving revision")


def deploy_runtime(image: str) -> dict[str, str]:
    service, region, project = (
        env("CGM_SERVICE"),
        env("CGM_REGION"),
        env("CGM_PROJECT_ID"),
    )
    previous = _traffic(service, region, project)
    previous_configs = [
        revision_runtime(revision) for revision, weight in previous.items() if weight
    ]
    previous_shas = {row.get("APP_RELEASE_SHA", "") for row in previous_configs}
    verify_artemis_web()
    candidate, url = _deploy_candidate(image, service, region, project)
    emit("validate-candidate", "running")
    runtime_ready(url, candidate=True)
    granted, promoted = False, False
    try:
        granted = True  # A timeout can occur after the management job committed.
        runtime_grant("authorize", env("CGM_RELEASE_SHA"))
        runtime_ready(url)
        emit(
            "validate-candidate",
            "succeeded",
            candidate_revision=candidate,
            candidate_url=url,
        )
        emit("promote", "running")
        promoted = True
        _set_traffic(service, region, project, {candidate: 100})
        emit("promote", "succeeded", production_revision=candidate)
        production = _service_url(service, region, project)
        emit("validate-production", "running")
        runtime_ready(production)
        for row in previous_configs:
            old_sha = row.get("APP_RELEASE_SHA", "")
            if row.get("APP_RELEASE_SCOPE") == "runtime-v1" and old_sha != env(
                "CGM_RELEASE_SHA"
            ):
                runtime_grant("retire", old_sha)
        emit(
            "validate-production",
            "succeeded",
            production_revision=candidate,
            production_url=production,
        )
        return {
            "candidate_revision": candidate,
            "candidate_url": url,
            "production_revision": candidate,
            "production_url": production,
            "image_digest": image,
        }
    except Exception as exc:
        emit(
            os.getenv("CGM_CURRENT_STAGE", "validate-candidate"),
            "failed",
            error=str(exc),
        )
        if promoted:
            for row in previous_configs:
                if row.get("APP_RELEASE_SCOPE") == "runtime-v1":
                    runtime_grant("authorize", row["APP_RELEASE_SHA"])
            _set_traffic(service, region, project, previous)
            if _traffic(service, region, project) != previous:
                raise RuntimeError(
                    "runtime rollback traffic verification failed"
                ) from exc
        if granted and env("CGM_RELEASE_SHA") not in previous_shas:
            runtime_grant("revoke", env("CGM_RELEASE_SHA"))
        if promoted:
            emit(
                "rollback",
                "succeeded",
                production_revision=max(previous, key=previous.get),
            )
            raise AutomaticRollback(
                f"runtime deployment failed; restored only {service}: {exc}"
            ) from exc
        raise


def rollback_runtime() -> dict[str, str]:
    target = env("CGM_TARGET_REVISION")
    values = revision_runtime(target)
    if values.get("APP_RELEASE_SCOPE") != "runtime-v1" or values.get(
        "APP_RELEASE_RESOURCE"
    ) != env("CGM_SERVICE"):
        raise RuntimeError(
            "rollback requires a compatible independent runtime revision"
        )
    sha = values.get("APP_RELEASE_SHA", "")
    previous = _traffic(env("CGM_SERVICE"), env("CGM_REGION"), env("CGM_PROJECT_ID"))
    verify_artemis_web()
    runtime_grant("authorize", sha)
    try:
        _set_traffic(
            env("CGM_SERVICE"), env("CGM_REGION"), env("CGM_PROJECT_ID"), {target: 100}
        )
        url = _service_url(env("CGM_SERVICE"), env("CGM_REGION"), env("CGM_PROJECT_ID"))
        runtime_ready(url, sha=sha)
    except Exception:
        _set_traffic(
            env("CGM_SERVICE"), env("CGM_REGION"), env("CGM_PROJECT_ID"), previous
        )
        raise
    emit("rollback", "succeeded", production_revision=target)
    return {"production_revision": target, "production_url": url}


ARTEMIS_JOB_SCHEDULES = {
    "cgm-artemis-fnd-observation-worker": ("cgm-artemis-fnd-observation-five-minutes",),
    "cgm-artemis-readings-export-worker": ("cgm-artemis-readings-export-five-minutes",),
    "cgm-artemis-smarti-prevention-worker": ("cgm-artemis-smarti-prevention-daytime",),
    "cgm-artemis-wm-sweep-worker": (
        "cgm-artemis-wm-sweep-hour",
        "cgm-artemis-wm-sweep-half",
    ),
}


def scheduler_command(action: str, name: str, *args: str) -> str:
    return run(
        "gcloud",
        "scheduler",
        "jobs",
        action,
        name,
        *args,
        "--location",
        env("CGM_REGION"),
        "--project",
        env("CGM_PROJECT_ID"),
        "--quiet",
    )


def pause_job_schedules() -> list[str]:
    active = []
    try:
        for name in ARTEMIS_JOB_SCHEDULES[env("CGM_SERVICE")]:
            info = json.loads(scheduler_command("describe", name, "--format=json"))
            if info.get("state") == "ENABLED":
                active.append(name)
                scheduler_command("pause", name)
    except Exception:
        resume_job_schedules(active)
        raise
    return active


def resume_job_schedules(names: list[str]) -> None:
    for name in names:
        scheduler_command("resume", name)


def check_job_runtime() -> None:
    run(
        "gcloud",
        "run",
        "jobs",
        "execute",
        env("CGM_SERVICE"),
        "--region",
        env("CGM_REGION"),
        "--project",
        env("CGM_PROJECT_ID"),
        "--wait",
        "--quiet",
    )


def job_runtime_environment(definition: str) -> dict[str, str]:
    containers = yaml.safe_load(definition)["spec"]["template"]["spec"]["template"][
        "spec"
    ]["containers"]
    return {
        item["name"]: item.get("value", "") for item in containers[0].get("env", [])
    }


def deploy(image: str) -> dict[str, str]:
    if independent_runtime():
        return deploy_runtime(image)
    service, region, project = (
        env("CGM_SERVICE"),
        env("CGM_REGION"),
        env("CGM_PROJECT_ID"),
    )
    previous = _traffic(service, region, project)
    os.environ["CGM_PREVIOUS_TRAFFIC"] = json.dumps(previous, sort_keys=True)
    profile = PROFILE_SPECS[env("CGM_PROFILE")]
    verify_hooks()
    os.environ["CGM_RESOLVED_IMAGE"] = image
    promoted = False
    side_effects_started = False
    try:
        if profile["pre_candidate_hooks"]:
            side_effects_started = True
        run_hooks("pre_candidate_hooks")
        candidate, candidate_url = _deploy_candidate(image, service, region, project)
        os.environ["CGM_CANDIDATE_REVISION"] = candidate
        os.environ["CGM_CANDIDATE_URL"] = candidate_url
        emit("validate-candidate", "running")
        _smoke(candidate_url)
        run_hooks("candidate_hooks")
        emit(
            "validate-candidate",
            "succeeded",
            candidate_revision=candidate,
            candidate_url=candidate_url,
        )
        if profile["pre_promote_hooks"]:
            side_effects_started = True
        run_hooks("pre_promote_hooks")
        emit("promote", "running")
        # A failed update may already have moved traffic. Always reconcile it.
        promoted = True
        _set_traffic(service, region, project, {candidate: 100})
        emit("promote", "succeeded", production_revision=candidate)
        emit("validate-production", "running")
        production_url = _service_url(service, region, project)
        os.environ["CGM_PRODUCTION_URL"] = production_url
        _smoke(production_url)
        run_hooks("post_promote_hooks")
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
    except Exception as exc:
        if not promoted and not side_effects_started:
            raise
        emit("rollback", "running")
        try:
            if profile["recovery_hook"]:
                hook(profile["recovery_hook"])
            elif promoted:
                _set_traffic(service, region, project, previous)
            current = _traffic(service, region, project)
            if not profile["recovery_hook"] and current != previous:
                raise RuntimeError("prior traffic was not restored exactly")
            restored = max(current, key=current.get)
        except Exception as restore_exc:
            emit("rollback", "failed", error="paired recovery could not be verified")
            raise RuntimeError(
                "release failed and paired recovery could not be verified"
            ) from restore_exc
        emit("rollback", "succeeded", production_revision=restored)
        raise AutomaticRollback(
            f"release failed; prior resources were restored: {sanitized_error(str(exc))}"
        ) from exc


def rollback() -> dict[str, str]:
    if os.getenv("CGM_RUNTIME_KIND") == "cloud_run_job":
        return rollback_job()
    if independent_runtime():
        return rollback_runtime()
    profile = PROFILE_SPECS[env("CGM_PROFILE")]
    verify_hooks()
    target = env("CGM_TARGET_REVISION")
    emit("rollback", "running")
    if profile["rollback_hook"]:
        hook(profile["rollback_hook"])
        current = _traffic(env("CGM_SERVICE"), env("CGM_REGION"), env("CGM_PROJECT_ID"))
        actual = max(current, key=current.get)
        emit("rollback", "succeeded", production_revision=actual)
        return {
            "production_revision": actual,
            "production_url": _service_url(
                env("CGM_SERVICE"), env("CGM_REGION"), env("CGM_PROJECT_ID")
            ),
        }
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
    """Pause enabled triggers and stage a definition that can only check readiness."""
    if "@sha256:" not in image:
        raise RuntimeError("Job deployment requires an immutable image")
    before = _job_definition()
    _job_operational_spec(before)
    _job_image()
    _save_job_definition(before)
    previous = job_runtime_environment(before)
    container = yaml.safe_load(before)["spec"]["template"]["spec"]["template"]["spec"][
        "containers"
    ][0]
    original_args = container.get("args", [])
    if (
        original_args[:2] != ["-m", "cgm_sanplat_param.worker"]
        or "--type" not in original_args
    ):
        raise RuntimeError("Job command does not match the approved worker profile")
    active = pause_job_schedules()
    granted = False
    resume_safe = False
    try:
        emit("deploy-candidate", "running")
        run(
            "gcloud",
            "run",
            "jobs",
            "update",
            env("CGM_SERVICE"),
            "--image",
            image,
            "--update-labels",
            f"commit-sha={env('CGM_RELEASE_SHA')}",
            *candidate_env_args(),
            "--args=" + ",".join([*original_args, "--check-runtime"]),
            "--region",
            env("CGM_REGION"),
            "--project",
            env("CGM_PROJECT_ID"),
            "--quiet",
        )
        # The profile sets APP_RUNTIME_CHECK_ONLY=true in the same update as
        # the image. A dispatcher invocation cannot start business work here.
        if _job_image() != image:
            raise RuntimeError("updated Job image does not match authorized digest")
        emit(
            "deploy-candidate",
            "succeeded",
            candidate_revision=image,
            image_digest=image,
        )
        emit("validate-candidate", "running")
        granted = True
        runtime_grant("authorize", env("CGM_RELEASE_SHA"))
        check_job_runtime()
        emit("validate-candidate", "succeeded", candidate_revision=image)
        emit("promote", "running")
        run(
            "gcloud",
            "run",
            "jobs",
            "update",
            env("CGM_SERVICE"),
            "--update-env-vars",
            "APP_RUNTIME_CHECK_ONLY=false",
            "--args=" + ",".join(original_args),
            "--region",
            env("CGM_REGION"),
            "--project",
            env("CGM_PROJECT_ID"),
            "--quiet",
        )
        revision = _save_job_definition(_job_definition())
        emit("promote", "succeeded", production_revision=revision)
        if previous.get("APP_RELEASE_SCOPE") == "runtime-v1" and previous.get(
            "APP_RELEASE_SHA"
        ) != env("CGM_RELEASE_SHA"):
            runtime_grant("retire", previous["APP_RELEASE_SHA"])
        emit("validate-production", "succeeded", production_revision=revision)
        resume_safe = True
        return {
            "candidate_revision": image,
            "production_revision": revision,
            "image_digest": image,
        }
    except Exception as exc:
        emit(
            os.getenv("CGM_CURRENT_STAGE", "deploy-candidate"), "failed", error=str(exc)
        )
        if previous.get("APP_RELEASE_SCOPE") == "runtime-v1":
            runtime_grant("authorize", previous["APP_RELEASE_SHA"])
        _replace_job(before)
        if _job_operational_spec(_job_definition()) != _job_operational_spec(before):
            raise RuntimeError(
                "Job definition restoration could not be verified"
            ) from exc
        if granted and previous.get("APP_RELEASE_SHA") != env("CGM_RELEASE_SHA"):
            runtime_grant("revoke", env("CGM_RELEASE_SHA"))
        emit("rollback", "succeeded")
        resume_safe = True
        raise AutomaticRollback(
            f"Job update failed; prior definition restored: {exc}"
        ) from exc
    finally:
        if resume_safe:
            resume_job_schedules(active)


def rollback_job() -> dict[str, str]:
    """Restore an audited compatible definition; never reactivate legacy jobs."""
    target_revision = env("CGM_TARGET_REVISION")
    if not re.fullmatch(r"job-[0-9a-f]{64}", target_revision):
        raise RuntimeError("invalid target Job definition fingerprint")
    before = _job_definition()
    with tempfile.NamedTemporaryFile(
        mode="w+", suffix=".yaml", encoding="utf-8"
    ) as file:
        run("gcloud", "storage", "cp", _job_snapshot_uri(target_revision), file.name)
        file.seek(0)
        target = file.read()
    if "job-" + hashlib.sha256(target.encode()).hexdigest() != target_revision:
        raise RuntimeError("target Job definition fingerprint mismatch")
    expected_spec = _job_operational_spec(target)
    values = job_runtime_environment(target)
    if values.get("APP_RELEASE_SCOPE") != "runtime-v1" or values.get(
        "APP_RELEASE_RESOURCE"
    ) != env("CGM_SERVICE"):
        raise RuntimeError("rollback requires a compatible independent Job definition")
    active = pause_job_schedules()
    emit("rollback", "running")
    resume_safe = False
    try:
        runtime_grant("authorize", values["APP_RELEASE_SHA"])
        check = yaml.safe_load(target)
        container = check["spec"]["template"]["spec"]["template"]["spec"]["containers"][
            0
        ]
        container["env"] = [
            item
            for item in container.get("env", [])
            if item["name"] != "APP_RUNTIME_CHECK_ONLY"
        ]
        container["env"].append({"name": "APP_RUNTIME_CHECK_ONLY", "value": "true"})
        container["args"] = [*container.get("args", []), "--check-runtime"]
        _replace_job(yaml.safe_dump(check))
        check_job_runtime()
        _replace_job(target)
        if _job_operational_spec(_job_definition()) != expected_spec:
            raise RuntimeError("restored Job definition differs from target")
        resume_safe = True
    except Exception:
        _replace_job(before)
        if _job_operational_spec(_job_definition()) != _job_operational_spec(before):
            raise RuntimeError("Job rollback recovery could not be verified")
        resume_safe = True
        raise
    finally:
        if resume_safe:
            resume_job_schedules(active)
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
    verify_hooks()
    emit("verify-release", "running")
    if env("CGM_OPERATION") != "rollback" or independent_runtime():
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
