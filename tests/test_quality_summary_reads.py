"""Avoid repeated pre-cache reads without weakening management barriers."""

from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from eng_platform_api.models import CatalogResponse, CatalogService
from eng_platform_api.routers import quality
from eng_platform_api.services import catalog, log_catalog
from tests.test_log_catalog import install_catalog, pin_private_catalog, record


def test_summary_reads_catalog_once_before_cache_lookup(monkeypatch, tmp_path):
    path = install_catalog(
        monkeypatch, tmp_path, [record("first-api"), record("second-api")]
    )
    pin_private_catalog(monkeypatch, path)
    monkeypatch.setattr(quality.config, "mock_mode", False)
    monkeypatch.setattr(quality, "_summary_cache", None)
    monkeypatch.setattr(quality.quality_store, "get_latest_report", lambda name: None)
    monkeypatch.setattr(
        quality.github_actions, "get_ci_quality_project", lambda service: None
    )
    reader = Mock(wraps=log_catalog.load_catalog)
    monkeypatch.setattr(log_catalog, "load_catalog", reader)
    result = quality.get_quality_summary()
    assert {project.service_name for project in result.projects} == {
        "first-api",
        "second-api",
    }
    # One fresh catalog snapshot, then one management barrier per provider read.
    assert reader.call_count == 3
    assert quality.get_quality_summary() == result
    # A cache hit still reads current catalog; it no longer reads it N more times.
    assert reader.call_count == 4


def test_summary_stale_managed_object_cannot_read_provider(monkeypatch):
    stale = CatalogService(
        service_name="was-managed",
        repository="owner/repo",
        owner="owner",
        project_id="test-project",
        region="us-central1",
    )
    monkeypatch.setattr(quality, "_summary_cache", None)
    monkeypatch.setattr(
        catalog,
        "get_services",
        lambda: CatalogResponse(services=[stale], total=1),
    )
    monkeypatch.setattr(
        log_catalog,
        "load_catalog",
        lambda: [
            {"service_name": "was-managed", "management_mode": "observability_only"}
        ],
    )
    provider = Mock(side_effect=AssertionError("Denied resource reached provider"))
    monkeypatch.setattr(quality.quality_store, "get_latest_report", provider)
    with pytest.raises(HTTPException) as caught:
        quality.get_quality_summary()
    assert caught.value.status_code == 409
    provider.assert_not_called()
