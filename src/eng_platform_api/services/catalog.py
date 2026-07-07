"""Service catalog — returns real application and service metadata.

Sources:
- Cloud Run API for live service inventory
- catalog/applications.example.yaml for app metadata
- Falls back to mock data when APIs are unavailable.
"""

import json
import pathlib
from typing import Optional

from google.cloud import run_v2

from ..config import config
from ..models import (
    Application, CatalogResponse, CloudRunService,
    FinOpsLabels, SonarQubeProject, ValidationTarget,
)

_CATALOG_PATH = pathlib.Path(__file__).resolve().parent.parent / "static_examples" / "mock_catalog.json"
_PROJECT_ID = "cgm-assistant-prod"
_REGION = "us-central1"


def _list_cloud_run_services() -> list[CloudRunService]:
    """Query real Cloud Run services from GCP."""
    if config.mock_mode:
        return _fallback_services()

    try:
        client = run_v2.ServicesClient()
        parent = f"projects/{_PROJECT_ID}/locations/{_REGION}"
        services = []
        for svc in client.list_services(request={"parent": parent}):
            services.append(CloudRunService(
                service_name=svc.name.split("/")[-1],
                project_id=_PROJECT_ID,
                region=_REGION,
            ))
        if services:
            return services
    except Exception:
        pass

    return _fallback_services()


def _fallback_services() -> list[CloudRunService]:
    """Hardcoded service list as fallback."""
    return [
        CloudRunService(service_name="cgm-sanplat-api", project_id=_PROJECT_ID, region=_REGION),
        CloudRunService(service_name="cgm-sanplat-web", project_id=_PROJECT_ID, region=_REGION),
        CloudRunService(service_name="cgm-bot-api", project_id=_PROJECT_ID, region=_REGION),
        CloudRunService(service_name="communications-ms", project_id=_PROJECT_ID, region=_REGION),
        CloudRunService(service_name="eng-platform-api", project_id=_PROJECT_ID, region=_REGION),
        CloudRunService(service_name="eng-platform-web", project_id=_PROJECT_ID, region=_REGION),
    ]


def _get_app_config() -> list[dict]:
    """Load application config from catalog or use defaults."""
    if _CATALOG_PATH.exists():
        data = json.loads(_CATALOG_PATH.read_text())
        return data.get("applications", [])
    return [
        {
            "id": "cgm-integration-platform", "name": "CGM Integration Platform",
            "repository": "diegomad14/parametrizacion-correos-cgm",
            "owner": "cgm", "cost_center": "cgm",
            "quality": {"enabled": True, "project_key": "cgm-sanplat-param"},
            "finops": {"app": "cgm-integration-platform", "env": "prod", "owner": "cgm", "cost_center": "cgm"},
            "validation_targets": [
                {"name": "Perseo", "type": "external_source", "description": "KPI data source"},
                {"name": "FND", "type": "external_source", "description": "FND data"},
                {"name": "SanPlat", "type": "external_source", "description": "SanPlat integration"},
            ],
        },
    ]


def get_applications() -> CatalogResponse:
    apps = []
    cloud_run_services = _list_cloud_run_services()
    configs = _get_app_config()

    # Map Cloud Run services to apps by prefix
    def match_services(prefixes: list[str]) -> list[CloudRunService]:
        return [s for s in cloud_run_services if any(s.service_name.startswith(p) for p in prefixes)]

    for cfg in configs:
        if cfg["id"] == "cgm-integration-platform":
            targets = match_services(["cgm-sanplat", "cgm-bot"])
        elif cfg["id"] == "communications-ms":
            targets = match_services(["communications"])
        elif cfg["id"] == "engineering-platform":
            targets = match_services(["eng-platform"])
        else:
            targets = []

        apps.append(Application(
            id=cfg["id"],
            name=cfg["name"],
            repository=cfg["repository"],
            owner=cfg["owner"],
            cost_center=cfg.get("cost_center", ""),
            release_targets=targets if targets else [],
            validation_targets=[
                ValidationTarget(**vt) for vt in cfg.get("validation_targets", [])
            ],
            quality=SonarQubeProject(**cfg.get("quality", {})),
            finops=FinOpsLabels(**cfg.get("finops", {})),
        ))

    if not apps:
        for svc in cloud_run_services:
            apps.append(Application(
                id=svc.service_name,
                name=svc.service_name.replace("-", " ").title(),
                repository="",
                owner="",
                cost_center="",
                release_targets=[svc],
                validation_targets=[],
                quality=SonarQubeProject(),
                finops=FinOpsLabels(),
            ))

    return CatalogResponse(applications=apps, total=len(apps))


def get_application(app_id: str) -> Optional[Application]:
    for app in get_applications().applications:
        if app.id == app_id:
            return app
    return None
