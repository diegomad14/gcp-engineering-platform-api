"""Service catalog — returns application and service metadata.

MVP: Returns mock data from static_examples/mock_catalog.json.
Production: Reads from catalog YAML files in the repo or GitHub API.
"""

import json
import pathlib
from typing import Optional

from ..config import config
from ..models import Application, CatalogResponse, CloudRunService, FinOpsLabels, SonarQubeProject, ValidationTarget

_MOCK_PATH = pathlib.Path(__file__).resolve().parent.parent / "static_examples" / "mock_catalog.json"


def _load_mock_catalog() -> list[Application]:
    if _MOCK_PATH.exists():
        data = json.loads(_MOCK_PATH.read_text())
        return [Application(**item) for item in data.get("applications", [])]
    return _default_mock_catalog()


def _default_mock_catalog() -> list[Application]:
    """Hardcoded fallback mock data."""
    return [
        Application(
            id="cgm-integration-platform",
            name="CGM Integration Platform",
            repository="diegomad14/parametrizacion-correos-cgm",
            owner="cgm",
            cost_center="cgm",
            release_targets=[
                CloudRunService(
                    service_name="cgm-sanplat-api",
                    project_id="cgm-assistant-prod",
                    region="us-central1",
                ),
                CloudRunService(
                    service_name="cgm-sanplat-web",
                    project_id="cgm-assistant-prod",
                    region="us-central1",
                ),
            ],
            validation_targets=[
                ValidationTarget(name="Perseo", type="external_source", description="KPI data source"),
                ValidationTarget(name="FND", type="external_source", description="Field Network Device data"),
                ValidationTarget(name="SanPlat", type="external_source", description="SanPlat integration"),
            ],
            quality=SonarQubeProject(enabled=True, project_key="cgm-sanplat-param"),
            finops=FinOpsLabels(app="cgm-integration-platform", env="prod", owner="cgm", cost_center="cgm"),
        ),
        Application(
            id="example-service",
            name="Example Service",
            repository="example-org/example-service",
            owner="example-team",
            cost_center="example-cc",
            release_targets=[
                CloudRunService(
                    service_name="example-api",
                    project_id="example-project",
                    region="us-central1",
                ),
            ],
            validation_targets=[],
            quality=SonarQubeProject(enabled=False, project_key=""),
            finops=FinOpsLabels(app="example-service", env="staging", owner="example-team", cost_center="example-cc"),
        ),
    ]


def get_applications() -> CatalogResponse:
    apps = _load_mock_catalog()
    return CatalogResponse(applications=apps, total=len(apps))


def get_application(app_id: str) -> Optional[Application]:
    apps = _load_mock_catalog()
    for app in apps:
        if app.id == app_id:
            return app
    return None
