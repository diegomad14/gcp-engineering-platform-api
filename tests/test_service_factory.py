"""Tests for Service Factory endpoints."""

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
    assert "application:" not in data["yaml_contract"]
    assert "service: test-api" in data["labels_manifest"]
    assert "app:" not in data["labels_manifest"]
    assert "gcp-service-release.yaml" in " ".join(data["checklist"])
    assert ".quality-gate.yml" in data["generated_files"]
    assert "SonarQube" not in data["yaml_contract"]
    assert "reusable-quality-gate.yml" in data["caller_pr_check"]
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
