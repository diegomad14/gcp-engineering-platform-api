"""Service catalog — independent service metadata and Cloud Run state.

Sources:
- Cloud Run API for live service inventory
- static service catalog for ownership and repository metadata
- Falls back to mock data when APIs are unavailable.
"""

import re
from functools import lru_cache
from threading import Lock
from time import monotonic
from typing import Optional

from google.cloud import run_v2

from ..config import config
from .repository_identity import repository_id, same_repository
from . import log_catalog
from ..models import (
    CatalogResponse,
    CatalogService,
    FinOpsLabels,
    InventorySource,
    ServiceDetail,
    ServiceDeploymentConfig,
    ServiceQualityConfig,
    ServiceLogsCapability,
    ServiceTraffic,
    ValidationTarget,
)

_DETAIL_CACHE_TTL_SECONDS = 30
_detail_cache: dict[str, tuple[float, ServiceDetail]] = {}
_detail_cache_lock = Lock()


@lru_cache(maxsize=1)
def _run_client() -> run_v2.ServicesClient:
    return run_v2.ServicesClient()


@lru_cache(maxsize=1)
def _job_client() -> run_v2.JobsClient:
    return run_v2.JobsClient()


def deployment_blockers(service: CatalogService) -> list[str]:
    """Return actionable reasons why a service cannot be deployed by the platform."""
    if service.management_mode != "managed":
        return [
            "Observation-only resource: deployment, rollback, release and secret changes are disabled"
        ]
    blockers: list[str] = []
    deployment = service.deployment
    required_fields = [
        ("repository", service.repository),
        ("project_id", service.project_id),
        ("region", service.region),
        ("deployment.image_name", deployment.image_name),
        ("deployment.artifact_repository", deployment.artifact_repository),
        ("deployment.build_context", deployment.build_context),
        ("deployment.dockerfile_path", deployment.dockerfile_path),
    ]
    if deployment.executor != "cloud_build":
        required_fields.append(("deployment.workflow_file", deployment.workflow_file))
    else:
        from .release_profiles import profile_for

        build = config.cloud_build
        if not build.enabled:
            blockers.append("Cloud Build executor is disabled")
        if service.service_name not in build.enabled_services:
            blockers.append("Cloud Build executor is not enabled for this resource")
        for name in (
            "project_id",
            "region",
            "service_account",
            "evidence_bucket",
            "callback_service_account",
        ):
            if not getattr(build, name):
                blockers.append(f"Cloud Build {name} is required")
        if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", build.executor_image):
            blockers.append("Cloud Build executor image must be pinned by digest")
        repository = build.repositories.get(service.service_name, "")
        if (
            not repository.startswith("projects/")
            or "/connections/" not in repository
            or "/repositories/" not in repository
        ):
            blockers.append("Cloud Build connected repository is missing or invalid")
        try:
            profile_for(service)
        except ValueError as exc:
            blockers.append(str(exc))
        if not blockers and not config.mock_mode:
            from .executor_readiness import availability

            blockers.extend(
                availability(service.repository or "", repository, build.executor_image)
            )
    if deployment.runtime_kind == "cloud_run_service":
        required_fields.append(("deployment.health_path", deployment.health_path))
    if not deployment.enabled:
        blockers.append("deployment.enabled is false")
    blockers.extend(
        f"{name} is required" for name, value in required_fields if not value
    )
    return blockers


def _get_service_config() -> list[dict]:
    """Read the single validated local authority; never fall back to empty."""
    return log_catalog.load_catalog()


def _catalog_service(cfg: dict) -> CatalogService:
    observation_only = cfg.get("management_mode") == "observability_only"
    quality_cfg = dict(cfg.get("quality", {}))
    deployment_cfg = dict(cfg.get("deployment", {}))
    # A renamed repository publishes one quality execution for the same SHA, so
    # both catalog names accept each other's evidence. Without the reverse
    # alias a legacy deploy of the still-serving SanPlat runtime could never
    # consume the evidence its own repository produced after the rename.
    owners = {
        1306114845: ["cgm-artemis-api", "cgm-sanplat-api"],
        1306114872: ["cgm-artemis-web", "cgm-sanplat-web"],
    }
    identity = repository_id(cfg["repository"]) if cfg["repository"] else None
    if identity in owners:
        quality_cfg["evidence_services"] = owners[identity]
    if cfg.get("management_mode", "managed") == "managed" and cfg[
        "service_name"
    ].startswith("cgm-artemis-"):
        if cfg["service_name"] not in {"cgm-artemis-api", "cgm-artemis-web"}:
            deployment_cfg["private_runtime"] = (
                deployment_cfg.get("runtime_kind") == "cloud_run_service"
            )
    if cfg.get("management_mode") == "observability_only":
        deployment_cfg = log_catalog.ObservationDeployment.model_validate(
            deployment_cfg
        ).model_dump()
    service = CatalogService(
        service_name=cfg["service_name"],
        management_mode=cfg.get("management_mode", "managed"),
        inventory_source=InventorySource(**cfg["inventory_source"])
        if cfg.get("inventory_source")
        else None,
        repository=cfg["repository"],
        owner=cfg["owner"],
        cost_center=cfg.get("cost_center", ""),
        project_id=cfg["project_id"],
        region=cfg["region"],
        environment=cfg.get("environment", "" if observation_only else "prod"),
        release_model=cfg.get(
            "release_model",
            "observability-only" if observation_only else "managed-release",
        ),
        release_policy=cfg.get("release_policy", "" if observation_only else "oss-v2"),
        validation_targets=[
            ValidationTarget(**vt) for vt in cfg.get("validation_targets", [])
        ],
        quality=ServiceQualityConfig(**quality_cfg),
        deployment=ServiceDeploymentConfig(**deployment_cfg),
        finops=FinOpsLabels(**cfg.get("finops", {})),
        operational_secrets=cfg.get("operational_secrets", []),
        infrastructure_resources=cfg.get("infrastructure_resources", []),
        logs=ServiceLogsCapability(
            enabled=cfg.get("logs", {}).get("enabled", False),
            configured="logs" in cfg,
        ),
    )
    blockers = deployment_blockers(service)
    return service.model_copy(
        update={"deployment_ready": not blockers, "deployment_blockers": blockers}
    )


def get_services(visible_services: set[str] | None = None) -> CatalogResponse:
    services = [
        _catalog_service(cfg)
        for cfg in _get_service_config()
        if visible_services is None or cfg["service_name"] in visible_services
    ]
    return CatalogResponse(services=services, total=len(services))


def get_service(service_name: str) -> Optional[CatalogService]:
    # Resolve locally before computing readiness. Looking up a denied inventory
    # identity must not evaluate managed entries or invoke their providers.
    for item in _get_service_config():
        if item["service_name"] == service_name:
            return _catalog_service(item)
    return None


def get_services_by_repository(repository: str) -> list[CatalogService]:
    return [
        _catalog_service(item)
        for item in _get_service_config()
        if item.get("management_mode", "managed") == "managed"
        and item["repository"]
        and same_repository(item["repository"], repository)
    ]


def _is_ready(service: object) -> bool:
    terminal = getattr(service, "terminal_condition", None)
    if terminal is not None:
        state = getattr(getattr(terminal, "state", None), "name", "")
        if state:
            return state == "CONDITION_SUCCEEDED"
    return any(
        getattr(condition, "type_", "") == "Ready"
        and getattr(getattr(condition, "state", None), "name", "")
        == "CONDITION_SUCCEEDED"
        for condition in getattr(service, "conditions", [])
    )


def get_service_detail(service_name: str) -> Optional[ServiceDetail]:
    service = get_service(service_name)
    if service is None:
        return None

    detail = ServiceDetail(**service.model_dump())
    if service.management_mode != "managed":
        # Inventory existence is timestamped evidence, not readiness. Do not
        # discover metadata, execute readiness checks or invent mock health.
        return detail
    if config.mock_mode:
        detail.status = "healthy"
        return detail

    with _detail_cache_lock:
        cached = _detail_cache.get(service_name)
        now = monotonic()
        if (
            cached
            and now - cached[0] < _DETAIL_CACHE_TTL_SECONDS
            and cached[1].model_dump(include=set(CatalogService.model_fields))
            == service.model_dump()
        ):
            return cached[1]

    try:
        if service.deployment.runtime_kind == "cloud_run_job":
            live_job = _job_client().get_job(
                name=(
                    f"projects/{service.project_id}/locations/{service.region}/"
                    f"jobs/{service.service_name}"
                )
            )
            ready = _is_ready(live_job) and not bool(live_job.reconciling)
            latest = getattr(live_job, "latest_created_execution", None)
            completion = getattr(getattr(latest, "completion_status", None), "name", "")
            detail.status = "healthy" if ready else "degraded"
            detail.last_job_execution = getattr(latest, "name", "") or ""
            detail.last_job_execution_status = completion
            detail.latest_ready_revision = (
                f"job-generation-{live_job.generation}" if ready else ""
            )
            detail.serving_revision = detail.latest_ready_revision
            containers = getattr(
                getattr(getattr(live_job, "template", None), "template", None),
                "containers",
                [],
            )
            if containers:
                detail.runtime_sha = next(
                    (
                        item.value
                        for item in containers[0].env
                        if item.name == "APP_RELEASE_SHA"
                    ),
                    "",
                )
            if not ready:
                detail.error = "Cloud Run Job definition is not Ready"
            with _detail_cache_lock:
                _detail_cache[service_name] = (monotonic(), detail)
            return detail
        client = _run_client()
        live = client.get_service(
            name=(
                f"projects/{service.project_id}/locations/{service.region}"
                f"/services/{service.service_name}"
            )
        )
        detail.status = "healthy" if _is_ready(live) else "degraded"
        detail.url = getattr(live, "uri", "") or ""
        latest_ready_revision = getattr(live, "latest_ready_revision", "") or ""
        detail.latest_ready_revision = latest_ready_revision.split("/")[-1]
        traffic = getattr(live, "traffic_statuses", None) or getattr(
            live, "traffic", []
        )
        detail.traffic = [
            ServiceTraffic(
                revision=getattr(target, "revision", "")
                or detail.latest_ready_revision,
                percent=int(getattr(target, "percent", 0) or 0),
                tag=getattr(target, "tag", "") or "",
            )
            for target in traffic
        ]
        serving = sorted(
            (target for target in detail.traffic if target.percent),
            key=lambda target: -target.percent,
        )
        if serving:
            detail.serving_revision = serving[0].revision.split("/")[-1]
            revision = run_v2.RevisionsClient().get_revision(
                name=(
                    f"projects/{service.project_id}/locations/{service.region}/services/"
                    f"{service.service_name}/revisions/{detail.serving_revision}"
                )
            )
            detail.runtime_sha = next(
                (
                    item.value
                    for item in revision.containers[0].env
                    if item.name == "APP_RELEASE_SHA"
                ),
                "",
            )
        if detail.status != "healthy":
            detail.error = "Cloud Run service is not Ready"
    except Exception as exc:
        detail.status = "degraded"
        detail.error = str(exc)
    with _detail_cache_lock:
        _detail_cache[service_name] = (monotonic(), detail)
    return detail
