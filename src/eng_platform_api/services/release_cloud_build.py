"""Economy Cloud Build transport for quality and release preparation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from google.auth import default
from google.auth.transport.requests import AuthorizedSession

from ..config import config
from ..models import CatalogService
from . import release_executions
from .quality_profiles import executor_image, planner_hash, profile_for

_API = "https://cloudbuild.googleapis.com/v1"
_UNCERTAIN_GRACE_SECONDS = 120


class ReleaseCloudBuildError(RuntimeError):
    pass


def _location() -> str:
    return (
        f"projects/{config.cloud_build.project_id}/locations/"
        f"{config.cloud_build.region}"
    )


def _session() -> AuthorizedSession:
    credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    return AuthorizedSession(credentials)


def _repository(service: CatalogService) -> str:
    value = config.cloud_build.repositories.get(service.service_name, "")
    if not value.startswith("projects/"):
        raise ReleaseCloudBuildError(
            f"Connected repository is not configured for {service.service_name}"
        )
    return value


def _require_config(service: CatalogService) -> None:
    settings = config.release_orchestrator
    if not settings.enabled:
        raise ReleaseCloudBuildError("Release orchestrator is disabled")
    if service.service_name not in {
        *settings.enabled_services,
        *settings.canary_services,
    }:
        raise ReleaseCloudBuildError("Service is not enabled for release orchestration")
    if not config.cloud_build.project_id or not settings.service_account:
        raise ReleaseCloudBuildError(
            "Cloud Build project and service account are required"
        )
    profile = profile_for(service)
    executor_image(profile)
    if "@sha256:" not in settings.release_planner_image:
        raise ReleaseCloudBuildError("Release planner image must be pinned by digest")
    if profile.spec.get("postgres") and "@sha256:" not in settings.postgres_image:
        raise ReleaseCloudBuildError("PostgreSQL image must be pinned by digest")


def _planner_retry_request(
    execution: dict[str, Any],
    service: CatalogService,
    *,
    substitutions: dict[str, str],
    common_env: list[str],
) -> dict[str, Any]:
    """Retry only release planning after exact immutable quality evidence passed."""
    retry_count = int(execution.get("planner_retry_count", 0) or 0)
    remediation_authorized = bool(
        retry_count == 2
        and execution.get("planner_remediation_retry_pending")
        and execution.get("planner_remediation_retry_image")
        == config.release_orchestrator.release_planner_image
        and execution.get("planner_remediation_retry_hash") == planner_hash()
    )
    contract_retry_authorized = bool(
        retry_count == 3
        and execution.get("planner_contract_retry_pending")
        and execution.get("planner_contract_retry_image")
        == config.release_orchestrator.release_planner_image
        and execution.get("planner_contract_retry_hash") == planner_hash()
        and execution.get("planner_remediation_retry_hash") == planner_hash()
    )
    if (
        execution.get("operation") != "main_release"
        or retry_count not in {1, 2, 3}
        or not execution.get("planner_retry_pending")
        or not execution.get("evidence_committed")
        or not execution.get("report_hash")
        or (retry_count == 2 and not remediation_authorized)
        or (retry_count == 3 and not contract_retry_authorized)
    ):
        raise ReleaseCloudBuildError("Planner-only recovery is not authorized")
    profile = profile_for(service)
    executor = executor_image(profile)
    planner = config.release_orchestrator.release_planner_image
    runtime_planner_hash = (
        str(execution["planner_contract_retry_hash"])
        if contract_retry_authorized
        else str(execution["planner_remediation_retry_hash"])
        if remediation_authorized
        else str(execution["planner_hash"])
    )
    planner_env = [
        *common_env,
        # The plan callback uses the original execution hash for the first
        # retry, then the separately authorized hash of the rolled-forward
        # planner image for image remediation/contract recovery.
        f"ENG_PLATFORM_RELEASE_PLANNER_HASH={runtime_planner_hash}",
        f"ENG_PLATFORM_RELEASE_PLANNER_IMAGE={planner}",
        "GIT_CONFIG_COUNT=1",
        "GIT_CONFIG_KEY_0=safe.directory",
        "GIT_CONFIG_VALUE_0=/workspace",
    ]
    control_volume = {"name": "release-control", "path": "/eng-platform-control"}
    planner_volume = {"name": "planner-output", "path": "/eng-platform-plan"}
    steps = [
        {
            "id": "prepare",
            "name": executor,
            "args": [
                "--mode",
                "prepare",
                "--service",
                service.service_name,
                "--source",
                "/workspace",
                "--control-dir",
                "/eng-platform-control",
            ],
            "env": common_env,
            "volumes": [control_volume],
        },
        {
            "id": "prepare-planner-volume",
            "name": executor,
            "entrypoint": "/bin/bash",
            "args": [
                "-euc",
                "install -d -m 0700 /eng-platform-plan; "
                "chown -R 1000:1000 /eng-platform-plan /eng-platform-control",
            ],
            "volumes": [planner_volume, control_volume],
            "waitFor": ["prepare"],
        },
        {
            "id": "release-plan",
            "name": planner,
            "args": [
                "--mode",
                "plan",
                "--source",
                "/workspace",
                "--output",
                "/eng-platform-plan/release-plan.json",
            ],
            "env": planner_env,
            "volumes": [planner_volume],
            "waitFor": ["prepare-planner-volume"],
        },
        {
            "id": "publish-release-plan",
            "name": planner,
            "args": [
                "--mode",
                "publish",
                "--manifest",
                "/eng-platform-plan/release-plan.json",
                "--control-dir",
                "/eng-platform-control",
            ],
            "env": common_env
            + [
                f"ENG_PLATFORM_RELEASE_PLANNER_HASH={runtime_planner_hash}",
                f"ENG_PLATFORM_RELEASE_PLANNER_IMAGE={planner}",
            ],
            "volumes": [planner_volume, control_volume],
            "waitFor": ["release-plan"],
        },
    ]
    return {
        "source": {
            "connectedRepository": {
                "repository": _repository(service),
                "revision": str(execution["head_sha"]),
            }
        },
        "steps": steps,
        "timeout": f"{profile.timeout_seconds}s",
        "options": {
            "machineType": "E2_STANDARD_2",
            "logging": "CLOUD_LOGGING_ONLY",
            "substitutionOption": "ALLOW_LOOSE",
        },
        "serviceAccount": config.release_orchestrator.service_account,
        "substitutions": substitutions,
        "tags": [
            "eng-platform",
            "economy",
            "release-quality",
            f"release-{execution['execution_id'][:48]}",
        ],
    }


def build_request(execution: dict[str, Any], service: CatalogService) -> dict[str, Any]:
    """Generate the only allowed build shape; no request-supplied commands exist."""
    _require_config(service)
    profile = profile_for(service)
    execution_id = str(execution["execution_id"])
    fingerprint_value = str(execution["fingerprint"])
    operation = str(execution["operation"])
    if operation not in {"pr_quality", "main_release"}:
        raise ReleaseCloudBuildError("Release operation is not supported")
    planner_retry_count = int(execution.get("planner_retry_count", 0) or 0)
    remediation_authorized = bool(
        operation == "main_release"
        and planner_retry_count == 2
        and execution.get("planner_retry_pending")
        and execution.get("planner_remediation_retry_pending")
        and execution.get("planner_remediation_retry_image")
        == config.release_orchestrator.release_planner_image
        and execution.get("planner_remediation_retry_hash") == planner_hash()
    )
    contract_retry_authorized = bool(
        operation == "main_release"
        and planner_retry_count == 3
        and execution.get("planner_contract_retry_pending")
        and execution.get("planner_contract_retry_image")
        == config.release_orchestrator.release_planner_image
        and execution.get("planner_contract_retry_hash") == planner_hash()
        and execution.get("planner_remediation_retry_hash") == planner_hash()
    )
    planner_recovery_authorized = remediation_authorized or contract_retry_authorized
    if (
        execution.get("service_name") != service.service_name
        or execution.get("repository") != service.repository
        or execution.get("profile_hash") != profile.fingerprint()
        or execution.get("executor_digest") != executor_image(profile)
        or (
            operation == "main_release"
            and execution.get("planner_hash") != planner_hash()
            and not planner_recovery_authorized
        )
    ):
        raise ReleaseCloudBuildError("Release build identity is not authorized")
    substitutions = {
        "_EXECUTION_ID": execution_id,
        "_REQUEST_FINGERPRINT": fingerprint_value,
        "_SERVICE_NAME": service.service_name,
        "_REPOSITORY": service.repository,
        "_HEAD_SHA": str(execution["head_sha"]),
        "_BASE_SHA": str(execution["base_sha"]),
        "_OPERATION": operation,
        "_PROFILE_SHA256": profile.fingerprint(),
        "_EXECUTOR_DIGEST": str(execution["executor_digest"]),
        "_PLANNER_SHA256": str(execution.get("planner_hash", "")),
        "_PLANNER_DIGEST": (
            config.release_orchestrator.release_planner_image
            if operation == "main_release"
            else ""
        ),
    }
    if planner_retry_count:
        substitutions["_PLANNER_RETRY_ATTEMPT"] = str(planner_retry_count)
    if contract_retry_authorized:
        substitutions["_PLANNER_IMAGE_SHA256"] = str(
            execution["planner_contract_retry_hash"]
        )
    common_env = [
        f"ENG_PLATFORM_RELEASE_EXECUTION_ID={execution_id}",
        f"ENG_PLATFORM_RELEASE_FINGERPRINT={fingerprint_value}",
        f"ENG_PLATFORM_RELEASE_SERVICE={service.service_name}",
        f"ENG_PLATFORM_RELEASE_REPOSITORY={service.repository}",
        f"ENG_PLATFORM_RELEASE_HEAD_SHA={execution['head_sha']}",
        f"ENG_PLATFORM_RELEASE_BASE_SHA={execution['base_sha']}",
        f"ENG_PLATFORM_RELEASE_OPERATION={operation}",
        f"ENG_PLATFORM_RELEASE_PROFILE_SHA256={profile.fingerprint()}",
        f"ENG_PLATFORM_API_URL={config.github.platform_api_url}",
        f"ENG_PLATFORM_EVIDENCE_BUCKET={config.quality.bucket}",
        "ENG_PLATFORM_PROVIDER_RUN_ID=$BUILD_ID",
    ]
    control_volume = {"name": "release-control", "path": "/eng-platform-control"}
    quality_volume = {"name": "quality-output", "path": "/eng-platform-output"}
    external_volume = {
        "name": "quality-external",
        "path": "/eng-platform-external",
    }
    planner_volume = {"name": "planner-output", "path": "/eng-platform-plan"}
    executor = executor_image(profile)
    if execution.get("planner_retry_pending"):
        return _planner_retry_request(
            execution,
            service,
            substitutions=substitutions,
            common_env=common_env,
        )
    steps: list[dict[str, Any]] = [
        {
            "id": "prepare",
            "name": executor,
            "args": [
                "--mode",
                "prepare",
                "--service",
                service.service_name,
                "--source",
                "/workspace",
                "--control-dir",
                "/eng-platform-control",
            ],
            "env": common_env,
            "volumes": [control_volume],
        }
    ]
    quality_dependencies = ["prepare"]
    if profile.spec.get("container_smoke"):
        steps.append(
            {
                "id": "container-smoke",
                "name": executor,
                "entrypoint": "/opt/eng-platform/api_container_smoke.sh",
                "args": ["/workspace", "/eng-platform-external"],
                "env": ["BUILD_ID=$BUILD_ID"],
                "volumes": [external_volume],
                "waitFor": ["prepare"],
            }
        )
        quality_dependencies.append("container-smoke")
    if profile.spec.get("postgres"):
        steps.append(
            {
                "id": "postgres",
                "name": executor,
                "entrypoint": "/bin/bash",
                "args": [
                    "-euo",
                    "pipefail",
                    "-c",
                    (
                        "docker run --detach --rm --name eng-platform-postgres "
                        "--network cloudbuild -e POSTGRES_PASSWORD=quality-only "
                        # Cloud Build treats single-dollar expressions as
                        # substitutions before the container starts. Escape
                        # this shell environment expansion so it reaches bash.
                        '-e POSTGRES_DB=wm_test "$$ENG_PLATFORM_POSTGRES_IMAGE"; '
                        "for attempt in $(seq 1 30); do "
                        "if docker exec eng-platform-postgres pg_isready -U postgres "
                        "-d wm_test >/dev/null 2>&1; then "
                        "for create_attempt in $(seq 1 10); do "
                        "if docker exec eng-platform-postgres createdb -U postgres "
                        "fnd_test >/dev/null 2>&1; then exit 0; fi; sleep 1; done; "
                        "fi; sleep 1; done; "
                        "docker exec eng-platform-postgres createdb -U postgres fnd_test"
                    ),
                ],
                "env": [
                    "ENG_PLATFORM_POSTGRES_IMAGE="
                    + config.release_orchestrator.postgres_image
                ],
                "waitFor": ["prepare"],
            }
        )
        quality_dependencies.append("postgres")
    quality_env = list(common_env)
    quality_cli = [
        "--mode",
        "run",
        "--service",
        service.service_name,
        "--source",
        "/workspace",
        "--scratch",
        "/tmp/eng-platform-quality",
        "--output-dir",
        "/eng-platform-output",
        "--external-dir",
        "/eng-platform-external",
    ]
    quality_step: dict[str, Any] = {
        "id": "quality",
        "name": executor,
        "args": quality_cli,
        "env": quality_env,
        "volumes": (
            [quality_volume, external_volume]
            if profile.spec.get("container_smoke")
            else [quality_volume]
        ),
        "waitFor": quality_dependencies,
    }
    if profile.spec.get("postgres"):
        quality_env.extend(
            [
                "FND_TEST_POSTGRES_DSN="
                "postgresql://postgres:quality-only@127.0.0.1:5432/fnd_test",
                "WM_TEST_POSTGRES_DSN="
                "postgresql://postgres:quality-only@127.0.0.1:5432/wm_test",
            ]
        )
        quality_step.update(
            {
                "entrypoint": "/bin/bash",
                "args": [
                    "-euo",
                    "pipefail",
                    "-c",
                    (
                        "socat TCP-LISTEN:5432,bind=127.0.0.1,fork,reuseaddr "
                        "TCP:eng-platform-postgres:5432 & proxy=$!; "
                        "trap 'kill $proxy >/dev/null 2>&1 || true' EXIT; "
                        'python3 /opt/eng-platform/quality_executor.py "$@"'
                    ),
                    "quality-with-postgres",
                    *quality_cli,
                ],
            }
        )
    steps.append(quality_step)
    steps.append(
        {
            "id": "publish-quality",
            "name": executor,
            "args": [
                "--mode",
                "publish",
                "--service",
                service.service_name,
                "--manifest",
                "/eng-platform-output/quality-result.json",
                "--control-dir",
                "/eng-platform-control",
            ],
            "env": common_env,
            "volumes": [quality_volume, control_volume],
            "waitFor": ["quality"],
        }
    )
    if operation == "main_release":
        planner_env = [
            *common_env,
            f"ENG_PLATFORM_RELEASE_PLANNER_HASH={planner_hash()}",
            "ENG_PLATFORM_RELEASE_PLANNER_IMAGE="
            + config.release_orchestrator.release_planner_image,
        ]
        # Cloud Build checks out /workspace as root, while the pinned planner
        # image runs as an unprivileged user. Git's ownership protection
        # otherwise rejects read-only commands such as `git rev-parse HEAD`.
        # Scope the exception to this one repository path and this one step.
        planner_git_env = [
            "GIT_CONFIG_COUNT=1",
            "GIT_CONFIG_KEY_0=safe.directory",
            "GIT_CONFIG_VALUE_0=/workspace",
        ]
        steps.extend(
            [
                {
                    "id": "prepare-planner-volume",
                    "name": executor,
                    "entrypoint": "/bin/bash",
                    "args": [
                        "-euc",
                        "install -d -m 0700 /eng-platform-plan; "
                        "chown -R 1000:1000 /eng-platform-plan /eng-platform-control",
                    ],
                    "volumes": [planner_volume, control_volume],
                    "waitFor": ["publish-quality"],
                },
                {
                    "id": "release-plan",
                    "name": config.release_orchestrator.release_planner_image,
                    "args": [
                        "--mode",
                        "plan",
                        "--source",
                        "/workspace",
                        "--output",
                        "/eng-platform-plan/release-plan.json",
                    ],
                    "env": [*planner_env, *planner_git_env],
                    "volumes": [planner_volume],
                    "waitFor": ["prepare-planner-volume"],
                },
                {
                    "id": "publish-release-plan",
                    "name": config.release_orchestrator.release_planner_image,
                    "args": [
                        "--mode",
                        "publish",
                        "--manifest",
                        "/eng-platform-plan/release-plan.json",
                        "--control-dir",
                        "/eng-platform-control",
                    ],
                    "env": planner_env,
                    "volumes": [planner_volume, control_volume],
                    "waitFor": ["release-plan"],
                },
            ]
        )
    return {
        "source": {
            "connectedRepository": {
                "repository": _repository(service),
                "revision": str(execution["head_sha"]),
            }
        },
        "steps": steps,
        "timeout": f"{profile.timeout_seconds}s",
        "options": {
            "machineType": "E2_STANDARD_2",
            "logging": "CLOUD_LOGGING_ONLY",
            "substitutionOption": "ALLOW_LOOSE",
        },
        "serviceAccount": config.release_orchestrator.service_account,
        "substitutions": substitutions,
        "tags": [
            "eng-platform",
            "economy",
            "release-quality",
            f"release-{execution_id[:48]}",
        ],
    }


def get_build(build_id: str) -> dict[str, Any]:
    response = _session().get(f"{_API}/{_location()}/builds/{build_id}", timeout=15)
    if response.status_code >= 300:
        raise ReleaseCloudBuildError("Unable to read release Cloud Build")
    return response.json()


def _expected_substitutions(execution: dict[str, Any]) -> dict[str, str]:
    operation = str(execution["operation"])
    expected = {
        "_EXECUTION_ID": str(execution["execution_id"]),
        "_REQUEST_FINGERPRINT": str(execution["fingerprint"]),
        "_SERVICE_NAME": str(execution["service_name"]),
        "_REPOSITORY": str(execution["repository"]),
        "_HEAD_SHA": str(execution["head_sha"]),
        "_BASE_SHA": str(execution["base_sha"]),
        "_EXECUTOR_DIGEST": str(execution["executor_digest"]),
        "_OPERATION": operation,
        "_PROFILE_SHA256": str(execution["profile_hash"]),
        "_PLANNER_SHA256": (
            str(execution["planner_hash"]) if operation == "main_release" else ""
        ),
        "_PLANNER_DIGEST": (
            config.release_orchestrator.release_planner_image
            if operation == "main_release"
            else ""
        ),
    }
    planner_retry_count = int(execution.get("planner_retry_count", 0) or 0)
    if planner_retry_count:
        expected["_PLANNER_RETRY_ATTEMPT"] = str(planner_retry_count)
    if planner_retry_count == 3 and execution.get("planner_contract_retry_pending"):
        expected["_PLANNER_IMAGE_SHA256"] = str(
            execution.get("planner_contract_retry_hash", "")
        )
    return expected


def _matching_build(execution: dict[str, Any]) -> dict[str, Any] | None:
    execution_id = str(execution["execution_id"])
    response = _session().get(
        f"{_API}/{_location()}/builds",
        params={"filter": f'tags="release-{execution_id[:48]}"', "pageSize": "20"},
        timeout=15,
    )
    if response.status_code >= 300:
        raise ReleaseCloudBuildError("Release build reconciliation failed")
    expected = _expected_substitutions(execution)
    expected_repository = config.cloud_build.repositories.get(
        str(execution["service_name"]), ""
    )
    for build in response.json().get("builds", []):
        substitutions = build.get("substitutions", {})
        source = build.get("source", {}).get("connectedRepository", {})
        if (
            expected_repository
            and source.get("repository") == expected_repository
            and source.get("revision") == execution.get("head_sha")
            and build.get("serviceAccount")
            == config.release_orchestrator.service_account
            and all(substitutions.get(key) == value for key, value in expected.items())
        ):
            return build
    return None


def _bind(execution_id: str, build: dict[str, Any]) -> dict[str, Any]:
    build_id = str(build.get("id", ""))
    if not build_id:
        raise ReleaseCloudBuildError("Cloud Build did not expose a build ID")
    return release_executions.bind_build(
        execution_id,
        build_id=build_id,
        provider_status=str(build.get("status", "QUEUED")),
        logs_url=str(build.get("logUrl", "")),
        build_name=str(build.get("name", "")),
        submission_reconciled_at=datetime.now(timezone.utc).isoformat(),
    )


def submit(execution_id: str, service: CatalogService) -> dict[str, Any]:
    """Submit at most once; uncertain results remain reconcilable."""
    execution = release_executions.get(execution_id)
    if execution is None:
        raise ReleaseCloudBuildError("Unknown release execution")
    if execution.get("provider") != "cloud_build":
        raise ReleaseCloudBuildError("Release execution is not Cloud Build managed")
    if execution.get("build_id"):
        return execution
    recovered = _matching_build(execution)
    if recovered:
        return _bind(execution_id, recovered)
    if not release_executions.claim_submission(execution_id):
        raise ReleaseCloudBuildError("Release build submission is being reconciled")
    request = build_request(execution, service)
    try:
        response = _session().post(
            f"{_API}/{_location()}/builds", json=request, timeout=30
        )
    except Exception as exc:
        release_executions.save(
            execution_id,
            status="unknown",
            uncertain_since=datetime.now(timezone.utc).isoformat(),
        )
        raise ReleaseCloudBuildError("Release build submission is uncertain") from exc
    if response.status_code >= 300:
        diagnostic = ""
        if response.status_code == 400:
            try:
                error = response.json().get("error", {})
                if isinstance(error, dict):
                    diagnostic = str(error.get("message", ""))[:500]
            except (TypeError, ValueError):
                pass
        fields: dict[str, Any] = {
            "status": "failed",
            "submission_status_code": int(response.status_code),
        }
        if diagnostic:
            fields["submission_error"] = diagnostic
        release_executions.save(execution_id, **fields)
        raise ReleaseCloudBuildError(
            f"Release build submission failed: {response.status_code}"
        )
    operation = response.json()
    build = operation.get("metadata", {}).get("build", operation)
    substitutions = build.get("substitutions", {})
    if not build.get("id") or substitutions.get(
        "_REQUEST_FINGERPRINT"
    ) != execution.get("fingerprint"):
        release_executions.save(
            execution_id,
            status="unknown",
            uncertain_since=datetime.now(timezone.utc).isoformat(),
        )
        raise ReleaseCloudBuildError("Release build identity is uncertain")
    return _bind(execution_id, build)


def reconcile_uncertain_submission(
    execution_id: str, service: CatalogService
) -> dict[str, Any]:
    """Bind a lost response or permit one retry after a bounded absence check."""
    execution = release_executions.get(execution_id)
    if execution is None:
        raise ReleaseCloudBuildError("Unknown release execution")
    if execution.get("provider") != "cloud_build":
        return execution
    if execution.get("build_id"):
        return execution
    if execution.get("status") not in {"submitting", "unknown"}:
        return execution

    recovered = _matching_build(execution)
    if recovered:
        return _bind(execution_id, recovered)

    uncertain_at = str(
        execution.get("uncertain_since") or execution.get("updated_at") or ""
    )
    try:
        age = (
            datetime.now(timezone.utc)
            - datetime.fromisoformat(uncertain_at.replace("Z", "+00:00"))
        ).total_seconds()
    except ValueError:
        return execution
    if age < _UNCERTAIN_GRACE_SECONDS:
        return execution
    if int(execution.get("reconciliation_attempts", 0)) >= 1:
        return execution

    release_executions.reconcile_submission_absent(execution_id)
    return submit(execution_id, service)


def cancel(execution_id: str) -> bool:
    execution = release_executions.get(execution_id)
    if not execution:
        return False
    if execution.get("operation") != "pr_quality":
        return False
    if not execution.get("build_id"):
        recovered = _matching_build(execution)
        if not recovered:
            return False
        execution = _bind(execution_id, recovered)
    response = _session().post(
        f"{_API}/{_location()}/builds/{execution['build_id']}:cancel", timeout=15
    )
    if response.status_code >= 300 and response.status_code != 409:
        raise ReleaseCloudBuildError("Unable to cancel superseded PR build")
    release_executions.save(execution_id, status="failed", superseded=True)
    return True
