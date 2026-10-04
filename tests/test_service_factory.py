"""Tests for Service Factory endpoints."""

from unittest import mock

from fastapi.testclient import TestClient

from eng_platform_api.main import app

client = TestClient(app)


def _payload(**overrides):
    return {
        "repository": "test-org/test-repository",
        "service_name": "test-api",
        "service_type": "api",
        "runtime": "python",
        "gcp_project": "test-project",
        "region": "us-central1",
        "owner": "test-team",
        "cost_center": "test-cc",
        "environment": "staging",
        "cloud_run_service_name": "test-api",
        "health_path": "/health",
        "openapi_path": "/openapi.json",
        "quality_profile": "python",
        "quality_working_directory": ".",
        "coverage_threshold": 70,
        "validation_targets": ["Perseo", "FND"],
        **overrides,
    }


def test_list_templates():
    response = client.get("/api/service-factory/templates")
    assert response.status_code == 200
    assert "cloud-run-api" in [template["name"] for template in response.json()]


def test_generate_service_plan():
    response = client.post("/api/service-factory/plan", json=_payload())
    assert response.status_code == 200
    data = response.json()
    assert data["repository"] == "test-org/test-repository"
    assert data["service_name"] == "test-api"
    assert "service:" in data["yaml_contract"]
    assert "# Repo: test-org/test-repository" in data["yaml_contract"]
    assert "application:" not in data["yaml_contract"]
    assert "service: test-api" in data["labels_manifest"]
    assert "app:" not in data["labels_manifest"]
    assert "gcp-service-release.yaml" in " ".join(data["checklist"])
    assert ".quality-gate.yml" in data["generated_files"]
    assert ".github/workflows/platform-deploy.yml" in data["generated_files"]
    assert ".github/workflows/platform-rollback.yml" in data["generated_files"]
    assert ".github/workflows/semantic-release.yml" in data["generated_files"]
    assert f"catalog/services/{data['service_name']}.yaml" in data["generated_files"]
    assert "agent-handoff-prompt.md" in data["generated_files"]
    assert "SonarQube" not in data["yaml_contract"]
    assert "reusable-quality-gate.yml" in data["caller_pr_check"]
    assert (
        "reusable-quality-gate.yml@bac46b1a70c0305470ed1d4c11a18bf07c09ab76"
        in data["caller_pr_check"]
    )
    assert "workflow_dispatch" in data["platform_deploy_workflow"]
    assert "github_deployment_id" in data["platform_deploy_workflow"]
    assert "target_revision" in data["platform_rollback_workflow"]
    assert "semantic-release" in data["semantic_release_workflow"]
    assert "service_name: test-api" in data["catalog_entry"]
    assert "Never use GCP Console" in data["agent_prompt"]
    assert data["sonar_properties"] == ""


def test_generate_plan_rejects_legacy_app_name():
    payload = _payload()
    payload["app_name"] = "legacy-app"
    response = client.post("/api/service-factory/plan", json=payload)
    assert response.status_code == 422


def test_generate_worker_plan():
    response = client.post(
        "/api/service-factory/plan",
        json=_payload(service_name="worker-proc", service_type="worker"),
    )
    assert response.status_code == 200
    assert response.json()["service_name"] == "worker-proc"


def test_generate_plan_returns_structured_error_when_template_is_missing():
    with mock.patch(
        "eng_platform_api.routers.service_factory.sf.generate_plan",
        side_effect=FileNotFoundError("missing template"),
    ):
        response = client.post("/api/service-factory/plan", json=_payload())
    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Service Factory templates are unavailable in this deployment"
    )


def test_job_plan_has_no_service_deployment_or_http_contract():
    import yaml
    from eng_platform_api.services.log_catalog import validate_catalog

    response = client.post(
        "/api/service-factory/plan",
        json=_payload(runtime_kind="cloud_run_job", service_name="batch-job"),
    )
    assert response.status_code == 200
    plan = response.json()
    entry = validate_catalog({"services": [yaml.safe_load(plan["catalog_entry"])]})[0]
    assert entry["deployment"]["runtime_kind"] == "cloud_run_job"
    assert entry["deployment"]["enabled"] is False
    assert entry["deployment"]["executor"] == "cloud_build"
    assert entry["deployment"]["health_path"] == ""
    assert "workflow_file" not in entry["deployment"]
    assert entry["logs"] == {"enabled": False, "allowed_logins": []}
    for field in (
        "caller_release_candidate",
        "caller_promote",
        "caller_rollback",
        "platform_deploy_workflow",
        "platform_rollback_workflow",
    ):
        assert plan[field] == ""
    contract = yaml.safe_load(plan["yaml_contract"])
    target = contract["release_target"]
    assert target["runtime_kind"] == "cloud_run_job"
    assert (
        not {"url", "health_path", "openapi_path", "expected_openapi_paths"}
        & target.keys()
    )
    assert "validation_targets" not in contract
    assert contract["deployment"]["enabled"] is False
    assert "job-specific release policy" in contract["deployment"]["review_required"]
    assert ".github/workflows/platform-deploy.yml" not in plan["generated_files"]
    assert "gcp-job-release.yaml" in plan["generated_files"]
    assert "cloud-run-job-labels.yaml" in plan["generated_files"]
    assert len(plan["generated_files"]) == 9


def test_default_service_catalog_is_valid_and_image_override_is_not_identity():
    import yaml
    from eng_platform_api.services.log_catalog import validate_catalog

    response = client.post(
        "/api/service-factory/plan",
        json=_payload(cloud_run_service_name="shared-image"),
    )
    assert response.status_code == 200
    plan = response.json()
    entry = validate_catalog({"services": [yaml.safe_load(plan["catalog_entry"])]})[0]
    assert entry["service_name"] == "test-api"
    assert entry["deployment"]["image_name"] == "shared-image"
    assert entry["deployment"]["runtime_kind"] == "cloud_run_service"
    assert entry["logs"] == {"enabled": False, "allowed_logins": []}
    assert (
        yaml.safe_load(plan["yaml_contract"])["release_target"]["service_name"]
        == "test-api"
    )
    assert any(
        "scripts/catalog_registry.py --check" in step for step in plan["checklist"]
    )
    assert any(
        "scripts/catalog_registry.py --write" in step for step in plan["checklist"]
    )
    assert any("actual shared source" in step for step in plan["checklist"])


def test_factory_cannot_accept_log_permissions_or_unknown_runtime_kind():
    for overrides in (
        {"logs": {"enabled": True, "allowed_logins": ["reader"]}},
        {"runtime_kind": "job"},
        {"quality_working_directory": "../escape"},
        {"service_name": "../../escape"},
        {"service_name": "test-api\nlogs: {enabled: true}"},
    ):
        response = client.post("/api/service-factory/plan", json=_payload(**overrides))
        assert response.status_code == 422


def test_contract_quotes_external_validation_target_names():
    import yaml

    name = "external\nlogs: {enabled: true}"
    response = client.post(
        "/api/service-factory/plan", json=_payload(validation_targets=[name])
    )
    assert response.status_code == 200
    contract = yaml.safe_load(response.json()["yaml_contract"])
    assert contract["validation_targets"]["smoke_endpoints"][0]["name"] == name
    assert "logs" not in contract


def test_published_request_schema_matches_factory_input_fields():
    import json
    from pathlib import Path
    from eng_platform_api.models import ServiceFactoryRequest

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "schemas/service-factory-request.schema.json"
        ).read_text()
    )
    assert set(schema["properties"]) == set(ServiceFactoryRequest.model_fields)
    assert schema["properties"]["runtime_kind"]["default"] == "cloud_run_service"
    assert schema["properties"]["cloud_run_service_name"]["deprecated"] is True
