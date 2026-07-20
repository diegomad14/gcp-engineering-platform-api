"""Regression tests for synchronous dependency isolation."""

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest import mock

from fastapi.testclient import TestClient

from eng_platform_api.main import app
from eng_platform_api.models import CatalogResponse, CatalogService, QualityProject


def test_slow_quality_dependency_does_not_block_auth():
    service = CatalogService(
        service_name="test-api",
        repository="test-org/test-api",
        owner="platform",
        project_id="test-project",
        region="us-central1",
    )
    dependency_started = Event()
    dependency_release = Event()

    def slow_project(_service):
        dependency_started.set()
        dependency_release.wait(timeout=2)
        return QualityProject(
            project_key="test-api",
            service_name="test-api",
            repository="test-org/test-api",
        )

    client = TestClient(app)
    with (
        mock.patch(
            "eng_platform_api.routers.quality.catalog.get_services",
            return_value=CatalogResponse(services=[service], total=1),
        ),
        mock.patch(
            "eng_platform_api.routers.quality._quality_project",
            side_effect=slow_project,
        ),
        mock.patch("eng_platform_api.routers.quality.config.mock_mode", True),
        ThreadPoolExecutor(max_workers=1) as executor,
    ):
        slow_response = executor.submit(client.get, "/api/quality/summary")
        assert dependency_started.wait(timeout=1)

        started = time.monotonic()
        auth_response = client.get("/api/auth/me")
        elapsed = time.monotonic() - started
        dependency_release.set()

        assert auth_response.status_code == 200
        assert elapsed < 0.2
        assert slow_response.result(timeout=2).status_code == 200
