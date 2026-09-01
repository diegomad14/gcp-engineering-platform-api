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
    assert "reusable-quality-gate.yml@v0.15.0" in data["caller_pr_check"]
    assert "workflow_dispatch" in data["platform_deploy_workflow"]
    assert "github_deployment_id" in data["platform_deploy_workflow"]
    assert "target_revision" in data["platform_rollback_workflow"]
    assert "semantic-release" in data["semantic_release_workflow"]
    assert "CGM_ACTIONS_RUNNER" in data["semantic_release_workflow"]
    assert (
        "format('cgm-release-local-{0}', github.sha)"
        in data["platform_deploy_workflow"]
    )
    assert (
        "format('cgm-release-local-{0}', github.sha)"
        in data["platform_rollback_workflow"]
    )
    assert "cgm-release-local" in " ".join(data["checklist"])
    assert "CGM_ACTIONS_RUNNER" in data["agent_prompt"]
    assert "disposable-VM" in data["agent_prompt"]
    assert "runner_label" in data["caller_promote"]
    assert "runner_label" in data["caller_rollback"]
    assert "runner_label: ${{ inputs.runner_label }}" in data["caller_promote"]
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
