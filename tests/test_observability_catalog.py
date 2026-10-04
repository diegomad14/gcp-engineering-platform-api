"""Inventory-only identities never imply ownership or managed-operation access."""

from copy import deepcopy
import json
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest
import yaml

from eng_platform_api.config import config
from eng_platform_api.main import app
from eng_platform_api.services import catalog, log_catalog
from scripts import catalog_registry
from tests.test_log_catalog import install_catalog, pin_private_catalog

OBSERVED_AT = "2020-01-01T00:00:00+00:00"


def observation(name="inventory-job", kind="cloud_run_job"):
    return {
        "management_mode": "observability_only",
        "service_name": name,
        "repository": None,
        "owner": None,
        "project_id": "example-project",
        "region": "us-central1",
        "environment": "",
        "release_model": "observability-only",
        "release_policy": "",
        "deployment": {"enabled": False, "runtime_kind": kind},
        "quality": {"enabled": False},
        "operational_secrets": [],
        "logs": {"enabled": False, "allowed_logins": []},
        "inventory_source": {
            "source": "cloud_run_inventory",
            "observed_at": OBSERVED_AT,
            "project": "example-project",
            "region": "us-central1",
        },
    }


def test_public_fixtures_have_51_resources_with_safe_management_split():
    rows = log_catalog.load_catalog()
    managed = [row for row in rows if row["management_mode"] == "managed"]
    observations = [
        row for row in rows if row["management_mode"] == "observability_only"
    ]
    assert len(rows) == 51 and len(managed) == 19 and len(observations) == 32
    assert (
        sum(row["deployment"]["runtime_kind"] == "cloud_run_service" for row in rows)
        == 22
    )
    assert (
        sum(row["deployment"]["runtime_kind"] == "cloud_run_job" for row in rows) == 29
    )
    assert all(row["repository"] and row["owner"] for row in managed)
    assert all(
        row["repository"] is None and row["owner"] is None for row in observations
    )
    for row in observations:
        assert row["inventory_source"] == {
            "source": "cloud_run_inventory",
            "observed_at": OBSERVED_AT,
            "project": "demo-platform-prod",
            "region": "us-central1",
        }
        assert row["deployment"]["enabled"] is False
        assert row["quality"]["enabled"] is False
        assert row["operational_secrets"] == []
        assert row["logs"] == {"enabled": False, "allowed_logins": []}
    assert all(row["logs"] == {"enabled": False, "allowed_logins": []} for row in rows)


@pytest.mark.parametrize("kind", ["cloud_run_service", "cloud_run_job"])
def test_observation_metadata_is_nullable_and_never_discovers_readiness(
    monkeypatch, tmp_path, kind
):
    path = install_catalog(monkeypatch, tmp_path, [observation(kind=kind)])
    forbidden = Mock(
        side_effect=AssertionError("No provider/readiness for inventory metadata")
    )
    monkeypatch.setattr(catalog, "_run_client", forbidden)
    monkeypatch.setattr(catalog, "_job_client", forbidden)
    from eng_platform_api.services import executor_readiness

    monkeypatch.setattr(executor_readiness, "availability", forbidden)
    for mock_mode in (True, False):
        monkeypatch.setattr(config, "mock_mode", mock_mode)
        if not mock_mode:
            pin_private_catalog(monkeypatch, path)
        response = TestClient(app).get("/api/catalog/services/inventory-job")
        if mock_mode:
            assert response.status_code == 200
            row = response.json()
        else:
            # Production metadata now requires a real authorized reader. Test
            # the underlying nullable projection without bypassing that guard.
            assert response.status_code == 401
            row = catalog.get_service_detail("inventory-job").model_dump()
        assert row["management_mode"] == "observability_only"
        assert row["repository"] is None and row["owner"] is None
        assert row["status"] == "unknown"
        assert row["deployment_ready"] is False and row["deployment_blockers"]
        assert row["deployment"]["workflow_file"] == ""
        assert row["deployment"]["image_name"] == ""
        assert row["logs"] == {"enabled": False, "configured": True}
        assert row["operational_secrets"] == []
        assert "allowed_logins" not in response.text
    assert catalog.get_services_by_repository("example/guessed") == []
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "path,value",
    [
        (("management_mode",), "unknown"),
        (("management_mode",), "managed"),
        (("repository",), "example/guessed"),
        (("owner",), "guessed-owner"),
        (("deployment", "enabled"), True),
        (("deployment", "enabled"), 0),
        (("deployment", "workflow_file"), "platform-deploy.yml"),
        (("deployment", "image_name"), "guessed-image"),
        (("deployment", "executor"), "cloud_build"),
        (("quality", "enabled"), True),
        (("quality", "enabled"), 0),
        (("quality", "evidence_services"), ["managed-api"]),
        (("operational_secrets",), [{"key": "API_KEY", "secret_id": "guess"}]),
        (("inventory_source", "source"), "guessed-name"),
        (("inventory_source", "observed_at"), "2020-01-01"),
        (("inventory_source", "observed_at"), "invalid"),
        (("inventory_source", "project"), "different-project"),
        (("inventory_source", "region"), "europe-west1"),
        (("environment",), "prod"),
        (("cost_center",), "guessed-center"),
        (("finops",), {"owner": "guessed"}),
        (("validation_targets",), [{"name": "guessed"}]),
    ],
)
def test_observation_contract_cannot_smuggle_managed_metadata(path, value):
    row = observation()
    target = row
    for name in path[:-1]:
        target = target[name]
    target[path[-1]] = value
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.validate_catalog({"services": [row]})


@pytest.mark.parametrize(
    "field",
    ["repository", "owner", "inventory_source", "project_id", "region", "deployment"],
)
def test_observation_coordinates_unknown_ownership_and_provenance_are_explicit(field):
    row = observation()
    del row[field]
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.validate_catalog({"services": [row]})


def test_inventory_registration_stays_observation_only_without_permission_grants(
    monkeypatch, tmp_path
):
    from tests.test_log_catalog import record

    path = install_catalog(monkeypatch, tmp_path, [record()])
    proposal = tmp_path / "inventory.yaml"
    proposal.write_text(yaml.safe_dump(observation()))
    before = path.read_bytes()
    summary = catalog_registry.register(proposal)
    assert summary.managed == 1 and summary.observability_only == 1
    assert path.read_bytes() == before
    catalog_registry.register(proposal, write=True)
    row = catalog.get_service("inventory-job")
    assert row.management_mode == "observability_only"
    assert not row.deployment_ready and row.repository is None
    assert not log_catalog.resources()[1].can_read("reader")
    # A new proposal cannot replace or upgrade an existing inventory identity.
    with pytest.raises(catalog_registry.RegistrationError, match="globally unique"):
        catalog_registry.register(proposal, write=True)


@pytest.mark.parametrize("kind", ["cloud_run_service", "cloud_run_job"])
def test_new_inventory_resource_does_not_inherit_approved_reader(
    monkeypatch, tmp_path, kind
):
    from tests.test_log_catalog import (
        install_synthetic_private_catalog,
        pin_private_catalog,
    )

    path = install_synthetic_private_catalog(monkeypatch, tmp_path)
    original = log_catalog.load_catalog()
    proposal = tmp_path / "inventory.yaml"
    proposal.write_text(yaml.safe_dump(observation(kind=kind)))
    catalog_registry.register(proposal, write=True)
    saved = log_catalog.load_catalog(path)
    assert len(saved) == 52 and saved[:51] == original
    assert saved[51]["logs"] == {"enabled": False, "allowed_logins": []}
    pin_private_catalog(monkeypatch, path)
    resources = log_catalog.resources()
    assert all(resource.can_read("demo-reader") for resource in resources[:51])
    assert not resources[51].can_read("demo-reader")
    assert catalog.get_service("inventory-job").management_mode == "observability_only"


def test_future_explicit_reader_policy_does_not_enable_managed_operations(
    monkeypatch, tmp_path
):
    row = observation()
    row["logs"] = {"enabled": True, "allowed_logins": ["reader"]}
    install_catalog(monkeypatch, tmp_path, [row])
    assert log_catalog.resources()[0].can_read("reader")
    assert catalog.get_service("inventory-job").management_mode == "observability_only"
    assert not catalog.get_service("inventory-job").deployment_ready
    proposal = tmp_path / "forbidden-policy.yaml"
    proposal.write_text(yaml.safe_dump(row))
    with pytest.raises(catalog_registry.RegistrationError, match="cannot grant"):
        catalog_registry.read_proposal(proposal)


def test_factory_cannot_convert_inventory_identity_into_deployment_proposal(
    monkeypatch, tmp_path
):
    from eng_platform_api.services import service_factory
    from tests.test_service_factory import _payload

    install_catalog(monkeypatch, tmp_path, [observation("test-api")])
    forbidden = Mock(
        side_effect=AssertionError("No artifact generation for inventory-only identity")
    )
    monkeypatch.setattr(service_factory, "_build_yaml_contract", forbidden)
    response = TestClient(app).post("/api/service-factory/plan", json=_payload())
    assert response.status_code == 409
    forbidden.assert_not_called()


def test_factory_authority_failure_is_sanitized(monkeypatch, tmp_path):
    from tests.test_service_factory import _payload

    monkeypatch.setattr(
        log_catalog, "CATALOG_PATH", tmp_path / "missing-sensitive.json"
    )
    response = TestClient(app).post("/api/service-factory/plan", json=_payload())
    assert response.status_code == 503
    assert response.json() == {"detail": "Runtime catalog is unavailable"}


def test_shared_schema_accepts_managed_and_observation_modes():
    from jsonschema import Draft202012Validator
    from pathlib import Path

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1] / "schemas/platform-catalog.schema.json"
        ).read_text()
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(
        json.loads(log_catalog.CATALOG_PATH.read_text())
    )
    invalid = deepcopy(observation())
    invalid["repository"] = "guessed/repo"
    assert list(Draft202012Validator(schema).iter_errors({"services": [invalid]}))


def test_observations_never_become_release_or_quality_repository_projections(
    monkeypatch, tmp_path
):
    from eng_platform_api.services import (
        github_actions,
        github_actions_quota,
        quality_policy,
    )
    from tests.test_quality_policy import payload

    path = install_catalog(monkeypatch, tmp_path, [observation()])
    pin_private_catalog(monkeypatch, path)
    service = catalog.get_service("inventory-job")
    forbidden = Mock(
        side_effect=AssertionError("No GitHub provider for unknown ownership")
    )
    monkeypatch.setattr(github_actions.github_deployments, "github_client", forbidden)
    monkeypatch.setattr(github_actions, "_fetch_recent_releases", forbidden)
    monkeypatch.setattr(github_actions_quota, "github_client", forbidden)
    monkeypatch.setattr(config, "mock_mode", False)
    assert github_actions.get_ci_quality_project(service) is None
    assert github_actions._release_items_for_repository("guessed/repo") == []
    assert github_actions.get_release_summary().recent == []
    assert github_actions_quota._private_repositories() == set()
    assert quality_policy.policy_errors(payload(), service) == [
        "Observation-only resource is not eligible for release quality"
    ]
    with pytest.raises(Exception) as blocked:
        github_actions._release_item(service, object(), [])
    assert blocked.value.status_code == 409
    forbidden.assert_not_called()


def test_metrics_projection_does_not_infer_managed_sampling_for_inventory(
    monkeypatch, tmp_path
):
    from eng_platform_api.services import gcp_monitoring
    from tests.test_log_catalog import record

    install_catalog(monkeypatch, tmp_path, [record("managed-api"), observation()])
    monkeypatch.setattr(gcp_monitoring, "_metrics_cache", {})
    sample = Mock(return_value=[])
    monkeypatch.setattr(gcp_monitoring, "get_metrics_for_services", sample)
    gcp_monitoring.get_metrics_summary()
    sample.assert_called_once_with(["managed-api"], minutes=1440)


def test_observation_lookup_does_not_evaluate_other_managed_readiness(
    monkeypatch, tmp_path
):
    from tests.test_log_catalog import record

    install_catalog(monkeypatch, tmp_path, [record("managed-api"), observation()])
    original = catalog.deployment_blockers

    def blockers(service):
        assert service.management_mode == "observability_only", (
            "Must resolve before managed readiness"
        )
        return original(service)

    monkeypatch.setattr(catalog, "deployment_blockers", blockers)
    assert catalog.get_service("inventory-job").management_mode == "observability_only"
    assert catalog.get_service("unknown") is None
    assert catalog.get_services_by_repository("not-registered/repo") == []


def test_minimal_observation_does_not_inherit_managed_metadata_defaults(
    monkeypatch, tmp_path
):
    row = observation()
    for field in (
        "environment",
        "release_model",
        "release_policy",
        "quality",
        "logs",
        "operational_secrets",
    ):
        row.pop(field)
    install_catalog(monkeypatch, tmp_path, [row])
    public = catalog.get_service("inventory-job")
    assert public.environment == ""
    assert public.release_model == "observability-only"
    assert public.release_policy == ""
    assert not public.logs.enabled and not public.logs.configured
    assert not public.quality.enabled
    assert public.operational_secrets == []
    assert public.deployment.workflow_file == "" and not public.deployment.enabled
    # The registration path must preserve those unknowns and add its explicit
    # denied policy; metadata defaults must not turn the proposal into adoption.
    row["service_name"] = "second-inventory-job"
    proposal = tmp_path / "minimal-observation.yaml"
    proposal.write_text(yaml.safe_dump(row))
    catalog_registry.register(proposal, write=True)
    saved = catalog.get_service("second-inventory-job")
    assert saved.environment == "" and saved.release_model == "observability-only"
    assert (
        saved.release_policy == "" and saved.logs.configured and not saved.logs.enabled
    )


@pytest.mark.parametrize("kind", ["cloud_run_service", "cloud_run_job"])
def test_multidigit_region_is_valid_for_future_factory_and_inventory(
    monkeypatch, tmp_path, kind
):
    from eng_platform_api.models import ServiceFactoryRequest
    from eng_platform_api.services import service_factory
    from tests.test_log_catalog import record

    row = observation(kind=kind)
    row["region"] = row["inventory_source"]["region"] = "europe-west12"
    install_catalog(monkeypatch, tmp_path, [record(), row])
    resource = next(
        r for r in log_catalog.resources() if r.service_id == "inventory-job"
    )
    assert resource.region == "europe-west12"
    plan = service_factory.generate_plan(
        ServiceFactoryRequest(
            repository="example/future",
            service_name="future-europe-resource",
            service_type="worker",
            runtime="python",
            runtime_kind=kind,
            gcp_project="example-project",
            region="europe-west12",
            owner="platform",
        )
    )
    proposal = tmp_path / "europe.yaml"
    proposal.write_text(plan.catalog_entry)
    catalog_registry.register(proposal, write=True)
    saved = catalog.get_service("future-europe-resource")
    assert saved.region == "europe-west12"
    assert saved.deployment.runtime_kind == kind
    assert not saved.logs.enabled
