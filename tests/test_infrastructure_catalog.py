"""Auxiliary identities are catalog metadata and never managed authorities."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
import yaml

from eng_platform_api.models import CatalogService, InfrastructureResource
from eng_platform_api.services import catalog, log_catalog, release_profiles


BASE = {
    "management_mode": "managed",
    "service_name": "test-service",
    "repository": "example/test-service",
    "owner": "example-team",
    "project_id": "test-project",
    "region": "us-central1",
    "deployment": {
        "runtime_kind": "cloud_run_service",
        "image_name": "test-service",
        "health_path": "/healthz",
    },
    "logs": {"enabled": False, "allowed_logins": []},
}
REFERENCE = {
    "resource_type": "gcs_bucket",
    "resource_name": "projects/_/buckets/test-attachments",
    "description": "30-day retention, metadata only",
}


def test_omitted_resource_field_preserves_catalog_and_public_shapes():
    original = deepcopy(BASE)
    before = hashlib.sha256(json.dumps(original, sort_keys=True).encode()).hexdigest()
    saved = log_catalog.validate_catalog({"services": [original]})[0]
    assert saved == original
    assert (
        hashlib.sha256(json.dumps(saved, sort_keys=True).encode()).hexdigest() == before
    )
    service = catalog._catalog_service(saved)
    assert service.infrastructure_resources == []
    assert "infrastructure_resources" not in service.model_dump()
    assert "infrastructure_resources" not in service.model_dump_json()
    with_empty = catalog._catalog_service({**saved, "infrastructure_resources": []})
    assert service.model_dump() == with_empty.model_dump()


def test_resources_reach_public_dto_without_adding_runtime_authority(monkeypatch):
    original = catalog._catalog_service(BASE)
    enriched = {**deepcopy(BASE), "infrastructure_resources": [REFERENCE]}
    validated = log_catalog.validate_catalog({"services": [enriched]})
    actual = catalog._catalog_service(validated[0])
    assert actual.model_dump()["infrastructure_resources"] == [REFERENCE]
    assert actual.deployment_ready == original.deployment_ready
    assert actual.deployment_blockers == original.deployment_blockers
    assert actual.deployment == original.deployment
    assert actual.quality == original.quality
    monkeypatch.setattr(log_catalog, "load_catalog", lambda: [BASE])
    before = log_catalog.resources()
    monkeypatch.setattr(log_catalog, "load_catalog", lambda: validated)
    after = log_catalog.resources()
    assert len(after) == 1
    assert after[0].key == before[0].key
    assert after[0].policy_fingerprint == before[0].policy_fingerprint
    assert not after[0].can_read("example-reader")
    assert after[0].kind == "cloud_run_service"


def test_serialization_schema_stays_structured_for_existing_api_clients():
    schema = CatalogService.model_json_schema(mode="serialization")
    assert "service_name" in schema["properties"]
    assert "deployment" in schema["properties"]
    assert "infrastructure_resources" in schema["properties"]


@pytest.mark.parametrize(
    "service_name", ["eng-platform-api", "communications-ms", "cgm-bot-api"]
)
def test_auxiliary_references_do_not_change_existing_release_profile_hashes(
    service_name,
):
    original = {**BASE, "service_name": service_name}
    before = release_profiles.profile_for(
        catalog._catalog_service(original)
    ).fingerprint()
    with_references = {**original, "infrastructure_resources": [REFERENCE]}
    after = release_profiles.profile_for(
        catalog._catalog_service(with_references)
    ).fingerprint()
    assert after == before


@pytest.mark.parametrize(
    "reference",
    [
        {**REFERENCE, "resource_type": "cloud_run_service"},
        {**REFERENCE, "resource_type": "arbitrary-operation"},
        {**REFERENCE, "resource_name": ""},
        {**REFERENCE, "resource_name": "two resource names"},
        {**REFERENCE, "value": "must-never-be-catalog-data"},
        {**REFERENCE, "logs": {"enabled": True}},
        {**REFERENCE, "deployment": {"enabled": True}},
    ],
)
def test_auxiliary_metadata_rejects_authority_and_unknown_fields(reference):
    with pytest.raises(ValidationError):
        InfrastructureResource.model_validate(reference)
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.validate_catalog(
            {"services": [{**BASE, "infrastructure_resources": [reference]}]}
        )


def test_auxiliary_list_is_bounded():
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.validate_catalog(
            {"services": [{**BASE, "infrastructure_resources": [REFERENCE] * 257}]}
        )


def test_observation_only_cannot_gain_auxiliary_managed_metadata():
    service = catalog._catalog_service(BASE).model_dump()
    service.update(
        {
            "management_mode": "observability_only",
            "repository": None,
            "owner": None,
            "inventory_source": {
                "source": "cloud_run_inventory",
                "observed_at": "2026-10-09T18:00:00Z",
                "project": "test-project",
                "region": "us-central1",
            },
            "infrastructure_resources": [REFERENCE],
        }
    )
    service["deployment"]["enabled"] = False
    with pytest.raises(ValidationError, match="Observation-only"):
        CatalogService.model_validate(service)


def test_reconnections_proposal_declares_only_real_auxiliary_identities():
    path = (
        Path(__file__).resolve().parents[1]
        / "catalog/services/cgm-reconnections-api.yaml"
    )
    proposal = yaml.safe_load(path.read_text())
    saved = log_catalog.validate_catalog({"services": [proposal]})[0]
    references = saved["infrastructure_resources"]
    assert len(references) == 13
    assert len({ref["resource_name"] for ref in references}) == 13
    assert {ref["resource_type"] for ref in references} == {
        "firestore_database",
        "gcs_bucket",
        "cloud_tasks_queue",
        "cloud_scheduler_job",
        "artifact_registry_repository",
        "secret_manager_secret",
        "billing_budget",
    }
    assert sum(ref["resource_type"] == "gcs_bucket" for ref in references) == 3
    assert (
        sum(ref["resource_type"] == "secret_manager_secret" for ref in references) == 5
    )
    assert len(saved["operational_secrets"]) == 5
    assert all(
        secret["editable"] is False
        for secret in catalog._catalog_service(saved).model_dump()[
            "operational_secrets"
        ]
    )
    assert saved["deployment"]["executor"] == "auto"
    assert saved["logs"] == {"enabled": False, "allowed_logins": []}
