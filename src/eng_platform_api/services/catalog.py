"""Service catalog — independent service metadata and Cloud Run state.

Sources:
- Cloud Run API for live service inventory
- static service catalog for ownership and repository metadata
- Falls back to mock data when APIs are unavailable.
"""

import json
import pathlib
from functools import lru_cache
from threading import Lock
from time import monotonic
from typing import Optional

from google.cloud import run_v2

from ..config import config
from ..models import (
    CatalogResponse,
    CatalogService,
    FinOpsLabels,
    ServiceDetail,
    ServiceDeploymentConfig,
    ServiceQualityConfig,
    ServiceTraffic,
    ValidationTarget,
)

_CATALOG_PATH = (
    pathlib.Path(__file__).resolve().parent.parent
    / "static_examples"
    / "mock_catalog.json"
)
_PROJECT_ID = "cgm-assistant-prod"
_REGION = "us-central1"
_DETAIL_CACHE_TTL_SECONDS = 30
_detail_cache: dict[str, tuple[float, ServiceDetail]] = {}
_detail_cache_lock = Lock()


@lru_cache(maxsize=1)
def _run_client() -> run_v2.ServicesClient:
    return run_v2.ServicesClient()


def deployment_blockers(service: CatalogService) -> list[str]:
    """Return actionable reasons why a service cannot be deployed by the platform."""
    blockers: list[str] = []
    deployment = service.deployment
    required_fields = [
        ("repository", service.repository),
        ("project_id", service.project_id),
        ("region", service.region),
        ("deployment.workflow_file", deployment.workflow_file),
        ("deployment.image_name", deployment.image_name),
        ("deployment.artifact_repository", deployment.artifact_repository),
        ("deployment.build_context", deployment.build_context),
        ("deployment.health_path", deployment.health_path),
    ]
    if not deployment.enabled:
        blockers.append("deployment.enabled is false")
    blockers.extend(
        f"{name} is required" for name, value in required_fields if not value
    )
    return blockers


def _get_service_config() -> list[dict]:
    """Load the flat service catalog."""
    if _CATALOG_PATH.exists():
        data = json.loads(_CATALOG_PATH.read_text())
        return data.get("services", [])
    return []


def _catalog_service(cfg: dict) -> CatalogService:
    service = CatalogService(
        service_name=cfg["service_name"],
        repository=cfg["repository"],
        owner=cfg["owner"],
        cost_center=cfg.get("cost_center", ""),
        project_id=cfg.get("project_id", _PROJECT_ID),
        region=cfg.get("region", _REGION),
        environment=cfg.get("environment", "prod"),
        validation_targets=[
            ValidationTarget(**vt) for vt in cfg.get("validation_targets", [])
        ],
        quality=ServiceQualityConfig(**cfg.get("quality", {})),
        deployment=ServiceDeploymentConfig(**cfg.get("deployment", {})),
        finops=FinOpsLabels(**cfg.get("finops", {})),
        operational_secrets=cfg.get("operational_secrets", []),
    )
    blockers = deployment_blockers(service)
    return service.model_copy(
        update={"deployment_ready": not blockers, "deployment_blockers": blockers}
    )


def get_services() -> CatalogResponse:
    services = [_catalog_service(cfg) for cfg in _get_service_config()]
    return CatalogResponse(services=services, total=len(services))


def get_service(service_name: str) -> Optional[CatalogService]:
    for service in get_services().services:
        if service.service_name == service_name:
            return service
    return None


def get_services_by_repository(repository: str) -> list[CatalogService]:
    return [
        service
        for service in get_services().services
        if service.repository == repository
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
    if config.mock_mode:
        detail.status = "healthy"
        return detail

    with _detail_cache_lock:
        cached = _detail_cache.get(service_name)
        now = monotonic()
        if cached and now - cached[0] < _DETAIL_CACHE_TTL_SECONDS:
            return cached[1]

    try:
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
        if detail.status != "healthy":
            detail.error = "Cloud Run service is not Ready"
    except Exception as exc:
        detail.status = "degraded"
        detail.error = str(exc)
    with _detail_cache_lock:
        _detail_cache[service_name] = (monotonic(), detail)
    return detail
