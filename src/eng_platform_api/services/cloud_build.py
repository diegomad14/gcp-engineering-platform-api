"""Inline, fixed-cost Cloud Build executor submission and reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import urllib.request
from typing import Any

from google.auth import default
from google.auth.transport.requests import AuthorizedSession

from ..config import config
from ..models import CatalogService, DeploymentItem
from . import deployment_executions
from .release_profiles import profile_for
from .repository_identity import aliases

_API = "https://cloudbuild.googleapis.com/v1"


class CloudBuildError(RuntimeError):
    pass


def _repository(service: CatalogService) -> str:
    value = config.cloud_build.repositories.get(service.service_name, "")
    if not value.startswith("projects/"):
        raise CloudBuildError(
            f"Cloud Build repository is not configured for {service.service_name}"
        )
    return value


def _require_config() -> None:
    settings = config.cloud_build
    if not settings.enabled:
        raise CloudBuildError("Cloud Build fallback is disabled")
    if settings.mode not in {"auto", "github_actions", "cloud_build"}:
        raise CloudBuildError("ENG_PLATFORM_DEPLOY_EXECUTOR_MODE is invalid")
    if not settings.project_id or not settings.service_account:
        raise CloudBuildError("Cloud Build project and service account are required")
    if "@sha256:" not in settings.executor_image:
        raise CloudBuildError("Cloud Build executor image must be pinned by digest")


def fingerprint(item: DeploymentItem, service: CatalogService) -> str:
    profile = profile_for(service)
    value = {
        "deployment_id": item.id,
        "repository": item.repository,
        "service": item.service_name,
        "tag": item.tag,
        "sha": item.sha,
        "kind": item.kind,
        "profile": profile.name,
        "profile_hash": profile.fingerprint(),
        "build_context": service.deployment.build_context,
        "dockerfile_path": service.deployment.dockerfile_path,
        "runtime_kind": service.deployment.runtime_kind,
        "private_runtime": service.deployment.private_runtime,
        "image_name": service.deployment.image_name,
        "quality_evidence_services": service.quality.evidence_services,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_request(item: DeploymentItem, service: CatalogService) -> dict[str, Any]:
    """Return an inline build with no quality suite or mutable build policy."""
    _require_config()
    profile = profile_for(service)
    request_fingerprint = fingerprint(item, service)
    substitutions = {
        "_DEPLOYMENT_ID": item.id,
        "_SERVICE_NAME": service.service_name,
        "_REPOSITORY": service.repository,
        "_RELEASE_TAG": item.tag,
        "_RELEASE_SHA": item.sha,
        "_OPERATION": item.kind,
        "_PROFILE": profile.name,
        "_PROFILE_SHA256": profile.fingerprint(),
        "_REQUEST_FINGERPRINT": request_fingerprint,
        "_IMAGE": (
            f"{service.region}-docker.pkg.dev/{service.project_id}/"
            f"{service.deployment.artifact_repository}/"
            f"{service.deployment.image_name}:{item.tag}"
        ),
    }
    return {
        "source": {
            "connectedRepository": {
                "repository": _repository(service),
                "revision": item.sha,
            }
        },
        "steps": [
            {
                "id": "central-release",
                "name": config.cloud_build.executor_image,
                # The image pins /usr/bin/python3 because the Cloud SDK base
                # ships an earlier interpreter on PATH without PyYAML.
                "entrypoint": "/usr/bin/python3",
                "args": [
                    "/opt/eng-platform/release_executor.py",
                    "--service=${_SERVICE_NAME}",
                ],
                "env": [
                    f"CGM_DEPLOYMENT_ID={item.id}",
                    f"CGM_OPERATION={item.kind}",
                    f"CGM_SERVICE={service.service_name}",
                    f"CGM_REPOSITORY={service.repository}",
                    f"CGM_REPOSITORY_ALIASES={','.join(aliases(service.repository))}",
                    f"CGM_RELEASE_TAG={item.tag}",
                    f"CGM_RELEASE_SHA={item.sha}",
                    f"CGM_PROFILE={profile.name}",
                    f"CGM_PROFILE_SHA256={profile.fingerprint()}",
                    "BUILD_ID=$BUILD_ID",
                    f"CGM_REQUEST_FINGERPRINT={request_fingerprint}",
                    f"CGM_IMAGE={substitutions['_IMAGE']}",
                    f"CGM_BUILD_CONTEXT={service.deployment.build_context}",
                    f"CGM_DOCKERFILE_PATH={service.deployment.dockerfile_path}",
                    f"CGM_RUNTIME_KIND={service.deployment.runtime_kind}",
                    f"CGM_PRIVATE_RUNTIME={'true' if service.deployment.private_runtime else 'false'}",
                    f"CGM_QUALITY_EVIDENCE_SERVICES={','.join(service.quality.evidence_services)}",
                    f"CGM_PAIRED_WEB_SHA={config.release_orchestrator.artemis_web_sha}",
                    f"CGM_HEALTH_PATH={service.deployment.health_path}",
                    f"CGM_TARGET_REVISION={item.production_revision}",
                    f"CGM_PROJECT_ID={service.project_id}",
                    f"CGM_REGION={service.region}",
                    f"CGM_ARTIFACT_REPOSITORY={service.deployment.artifact_repository}",
                    f"CGM_IMAGE_NAME={service.deployment.image_name}",
                    f"CGM_PLATFORM_API_URL={config.github.platform_api_url}",
                    f"CGM_EVIDENCE_BUCKET={config.cloud_build.evidence_bucket}",
                ],
            }
        ],
        "timeout": f"{profile.timeout_seconds}s",
        "options": {
            "machineType": "E2_STANDARD_2",
            "logging": "CLOUD_LOGGING_ONLY",
            # Identity fields intentionally remain recorded as substitutions
            # even when the executor receives their already-validated values
            # through env. Reconciliation verifies those immutable fields.
            "substitutionOption": "ALLOW_LOOSE",
        },
        "serviceAccount": config.cloud_build.service_account,
        "substitutions": substitutions,
        "tags": ["eng-platform", "economy", f"deployment-{item.id}"],
    }


def _session() -> AuthorizedSession:
    credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(credentials)


def _error_status(response: Any) -> str:
    """Return only the stable Google API status, never an arbitrary body."""
    try:
        value = str(response.json().get("error", {}).get("status", ""))
    except Exception:
        return ""
    return value if value.replace("_", "").isalnum() else ""


def _matching_build(
    item: DeploymentItem, request_fingerprint: str
) -> dict[str, Any] | None:
    """Recover a possibly accepted submission without creating a second build."""
    location = f"projects/{config.cloud_build.project_id}/locations/{config.cloud_build.region}"
    response = _session().get(
        f"{_API}/{location}/builds",
        params={"filter": f'tags="deployment-{item.id}"', "pageSize": "20"},
        timeout=15,
    )
    if response.status_code >= 300:
        raise CloudBuildError("Cloud Build submission reconciliation failed")
    expected_repository = config.cloud_build.repositories.get(item.service_name, "")
    for build in response.json().get("builds", []):
        substitutions = build.get("substitutions", {})
        source = build.get("source", {}).get("connectedRepository", {})
        if (
            expected_repository
            and source.get("repository") == expected_repository
            and source.get("revision") == item.sha
            and build.get("serviceAccount") == config.cloud_build.service_account
            and substitutions.get("_REQUEST_FINGERPRINT") == request_fingerprint
            and substitutions.get("_DEPLOYMENT_ID") == item.id
            and substitutions.get("_SERVICE_NAME") == item.service_name
            and substitutions.get("_REPOSITORY") == item.repository
            and substitutions.get("_RELEASE_TAG") == item.tag
            and substitutions.get("_RELEASE_SHA") == item.sha
            and substitutions.get("_OPERATION") == item.kind
        ):
            return build
    return None


def _bind_build(item: DeploymentItem, build: dict[str, Any]) -> DeploymentItem:
    build_id = build.get("id", "")
    if not build_id:
        raise CloudBuildError("Cloud Build did not expose a build ID")
    log_url = build.get("logUrl", "")
    deployment_executions.save(
        item.id,
        status=build.get("status", "QUEUED"),
        build_id=build_id,
        log_url=log_url,
        build_name=build.get("name", ""),
    )
    item.logs_url = log_url
    item.status = "QUEUED"
    item.current_stage = "queued"
    return item


def submit(
    item: DeploymentItem, service: CatalogService, *, reason: str
) -> DeploymentItem:
    """Submit once.  Any uncertain response is recoverable by fingerprint."""
    request = build_request(item, service)
    request_fingerprint = request["substitutions"]["_REQUEST_FINGERPRINT"]
    existing = deployment_executions.reserve(
        item.id,
        provider="cloud_build",
        fingerprint=request_fingerprint,
        service_name=item.service_name,
        repository=item.repository,
        sha=item.sha,
        tag=item.tag,
        kind=item.kind,
        reason=reason,
    )
    if existing.get("build_id"):
        item.logs_url = existing.get("log_url", item.logs_url)
        return item
    recovered = _matching_build(item, request_fingerprint)
    if recovered:
        return _bind_build(item, recovered)
    if not deployment_executions.claim_submission(item.id):
        raise CloudBuildError("Cloud Build submission is still being reconciled")
    location = f"projects/{config.cloud_build.project_id}/locations/{config.cloud_build.region}"
    try:
        response = _session().post(
            f"{_API}/{location}/builds", json=request, timeout=30
        )
    except Exception as exc:
        deployment_executions.save(item.id, status="SUBMISSION_UNKNOWN")
        raise CloudBuildError("Cloud Build submission response was uncertain") from exc
    if response.status_code >= 300:
        error_status = _error_status(response)
        deployment_executions.save(
            item.id,
            status="SUBMISSION_FAILED",
            submission_error_code=error_status,
        )
        detail = f" ({error_status})" if error_status else ""
        raise CloudBuildError(
            f"Managed executor submission failed: {response.status_code}{detail}"
        )
    operation = response.json()
    build = operation.get("metadata", {}).get("build", operation)
    build_id = build.get("id")
    if (
        not build_id
        or build.get("substitutions", {}).get("_REQUEST_FINGERPRINT")
        != request_fingerprint
    ):
        deployment_executions.save(item.id, status="SUBMISSION_UNKNOWN")
        raise CloudBuildError("Cloud Build submission identity is uncertain")
    return _bind_build(item, build)


def refresh(item: DeploymentItem) -> DeploymentItem:
    execution = deployment_executions.get(item.id)
    if not execution or not execution.get("build_id"):
        return item
    try:
        build = get_build(execution["build_id"])
    except CloudBuildError:
        return item
    state = build.get("status", "")
    item.logs_url = build.get("logUrl", item.logs_url)
    deployment_executions.save(item.id, status=state, log_url=item.logs_url)
    if state in {
        "SUCCESS",
        "FAILURE",
        "TIMEOUT",
        "CANCELLED",
        "EXPIRED",
        "INTERNAL_ERROR",
    }:
        _record_build_timing(item.id, build)
    if state == "SUCCESS":
        # A successful build is not enough to assert a successful deployment.
        # The authenticated final engine event is authoritative; absent that,
        # reconciliation must inspect the summary and runtime before unblocking.
        if item.status not in {"SUCCEEDED", "ROLLED_BACK"}:
            _reconcile(item, build)
    elif state in {"FAILURE", "TIMEOUT", "CANCELLED", "EXPIRED", "INTERNAL_ERROR"}:
        if item.status not in {"FAILED", "ROLLED_BACK", "ROLLBACK_FAILED"}:
            _reconcile(item, build)
    elif state in {"QUEUED", "PENDING"}:
        item.status, item.current_stage = "QUEUED", "queued"
    else:
        item.status, item.current_stage = "BUILDING", "build"
    return item


def _runtime_service(item: DeploymentItem) -> dict[str, Any]:
    service = profile_for_service(item.service_name)
    name = (
        f"projects/{service.project_id}/locations/{service.region}/"
        f"services/{service.service_name}"
    )
    response = _session().get(f"https://run.googleapis.com/v2/{name}", timeout=15)
    if response.status_code >= 300:
        raise CloudBuildError("Cloud Run reconciliation failed")
    return response.json()


def profile_for_service(service_name: str) -> CatalogService:
    # Imported lazily to keep catalog loading out of request construction.
    from . import catalog

    service = catalog.get_service(service_name)
    if service is None:
        raise CloudBuildError(f"Unknown release service {service_name}")
    return service


def _healthy(url: str, health_path: str) -> bool:
    if not url:
        return False
    target = url.rstrip("/") + "/" + health_path.lstrip("/")
    try:
        with urllib.request.urlopen(target, timeout=20) as response:
            return 200 <= response.status < 300
    except Exception:
        return False


def _record_build_timing(deployment_id: str, build: dict[str, Any]) -> None:
    """Account one completed deployment build, including retries and queueing."""
    started = str(build.get("startTime", ""))
    finished = str(build.get("finishTime", ""))
    if not started or not finished:
        return
    try:
        seconds = max(
            0.0,
            (_parse_time(finished) - _parse_time(started)).total_seconds(),
        )
    except (TypeError, ValueError):
        return
    created = str(build.get("createTime", ""))
    queue_seconds = 0.0
    if created:
        try:
            queue_seconds = max(
                0.0, (_parse_time(started) - _parse_time(created)).total_seconds()
            )
        except (TypeError, ValueError):
            queue_seconds = 0.0
    minutes = seconds / 60
    execution = deployment_executions.get(deployment_id) or {}
    attempt = {
        "build_id": str(build.get("id", "")),
        "duration_seconds": round(seconds, 3),
        "queue_seconds": round(queue_seconds, 3),
        "minutes": round(minutes, 3),
        "estimated_cost_usd": round(
            minutes * config.release_orchestrator.build_minute_price_usd, 6
        ),
        "finished_at": finished,
    }
    attempts = [
        item
        for item in execution.get("build_attempts", [])
        if item.get("build_id") != attempt["build_id"]
    ]
    attempts.append(attempt)
    deployment_executions.save(
        deployment_id,
        build_duration_seconds=round(seconds, 3),
        build_queue_seconds=round(queue_seconds, 3),
        build_attempts=attempts,
        build_minutes_estimate=round(
            sum(float(item.get("minutes", 0)) for item in attempts), 3
        ),
        estimated_compute_cost_usd=round(
            sum(float(item.get("estimated_cost_usd", 0)) for item in attempts), 6
        ),
        build_minute_price_usd=config.release_orchestrator.build_minute_price_usd,
        cost_category="deployment",
        build_finished_at=finished,
    )


def _parse_time(value: str):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _reconcile(item: DeploymentItem, build: dict[str, Any]) -> None:
    """Resolve a terminal build from real traffic when its callback was lost."""
    try:
        service = profile_for_service(item.service_name)
        if service.deployment.runtime_kind == "cloud_run_job":
            _reconcile_job(item, service, build)
            return
        runtime = _runtime_service(item)
        traffic = runtime.get("trafficStatuses", [])
        hundred = next(
            (
                row.get("revision", "")
                for row in traffic
                if int(row.get("percent", 0)) == 100 and row.get("revision")
            ),
            "",
        )
        expected = (
            item.production_revision
            if item.kind == "rollback"
            else f"{item.service_name}-ep-{fingerprint(item, service)[:10]}"
        )
        runtime_url = runtime.get("uri", "")
        if hundred == expected and _healthy(
            runtime_url, service.deployment.health_path
        ):
            item.production_revision = expected
            item.production_url = runtime_url
            item.status = "ROLLED_BACK" if item.kind == "rollback" else "SUCCEEDED"
            item.current_stage = "rollback" if item.kind == "rollback" else "complete"
            item.error = ""
            return
        if hundred != expected:
            item.status = "FAILED"
            item.current_stage = "reconcile"
            item.error = f"Cloud Build {build.get('status', '').lower()}; production traffic unchanged"
            return
        item.status = "UNKNOWN"
        item.current_stage = "reconcile"
        item.error = (
            "Expected revision is live but production smoke could not be verified"
        )
    except Exception:
        item.status = "UNKNOWN"
        item.current_stage = "reconcile"
        item.error = "Cloud Build completed but runtime state is indeterminate"


def _reconcile_job(
    item: DeploymentItem, service: CatalogService, build: dict[str, Any]
) -> None:
    """Require a successful build, immutable summary and actual Job image."""
    if build.get("status") != "SUCCESS":
        item.status = "UNKNOWN"
        item.current_stage = "reconcile"
        item.error = "Job build failed; restored definition needs verification"
        return
    from google.cloud import run_v2, storage

    bucket = storage.Client(project=service.project_id).bucket(
        config.cloud_build.evidence_bucket
    )
    blob = bucket.blob(f"deployment-summaries/{item.id}.json")
    summary = json.loads(blob.download_as_text())
    if (
        summary.get("deployment_id") != item.id
        or summary.get("fingerprint") != fingerprint(item, service)
        or summary.get("service") != item.service_name
        or summary.get("sha") != item.sha
        or summary.get("tag") != item.tag
        or not str(summary.get("production_revision", "")).startswith("job-")
        or "@sha256:" not in str(summary.get("image_digest", ""))
    ):
        raise CloudBuildError("Job summary identity does not match build")
    job = run_v2.JobsClient().get_job(
        name=(
            f"projects/{service.project_id}/locations/{service.region}/"
            f"jobs/{service.service_name}"
        )
    )
    containers = job.template.template.containers
    if len(containers) != 1 or containers[0].image != summary["image_digest"]:
        raise CloudBuildError("Job image does not match completed release")
    item.production_revision = str(summary["production_revision"])
    item.status = "ROLLED_BACK" if item.kind == "rollback" else "SUCCEEDED"
    item.current_stage = "rollback" if item.kind == "rollback" else "complete"
    item.error = ""


def get_build(build_id: str) -> dict[str, Any]:
    """Read a regional build and fail closed when Cloud Build rejects it."""
    location = f"projects/{config.cloud_build.project_id}/locations/{config.cloud_build.region}"
    response = _session().get(f"{_API}/{location}/builds/{build_id}", timeout=15)
    if response.status_code >= 300:
        raise CloudBuildError(f"Cloud Build read failed: {response.status_code}")
    return response.json()
