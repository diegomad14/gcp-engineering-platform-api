"""Tests for Service Factory endpoints."""

from fastapi.testclient import TestClient

from eng_platform_api.main import app

client = TestClient(app)


def test_list_templates():
    response = client.get("/api/service-factory/templates")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 2
    template_names = [t["name"] for t in data]
    assert "cloud-run-api" in template_names


def test_generate_plan():
    payload = {
        "app_name": "test-service",
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
        "sonar_project_key": "test-org_test-service",
        "validation_targets": ["Perseo", "FND"],
    }
    response = client.post("/api/service-factory/plan", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["app_name"] == "test-service"
    assert len(data["generated_files"]) >= 4
    assert len(data["checklist"]) >= 5
    assert "GCP_SA_KEY" in data["yaml_contract"]
    assert "reusable-pr-check.yml" in data["caller_pr_check"]


def test_generate_plan_minimal():
    payload = {
        "app_name": "minimal-service",
        "service_name": "minimal-api",
        "service_type": "api",
        "runtime": "python",
        "gcp_project": "test-project",
        "owner": "test-team",
        "cost_center": "test-cc",
        "environment": "dev",
        "cloud_run_service_name": "minimal-api",
    }
    response = client.post("/api/service-factory/plan", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["app_name"] == "minimal-service"


def test_generate_plan_worker():
    payload = {
        "app_name": "worker-service",
        "service_name": "worker-proc",
        "service_type": "worker",
        "runtime": "python",
        "gcp_project": "test-project",
        "owner": "test-team",
        "cost_center": "test-cc",
        "environment": "prod",
        "cloud_run_service_name": "worker-proc",
    }
    response = client.post("/api/service-factory/plan", json=payload)
    assert response.status_code == 200
    assert "caller_pr_check" in response.json()
