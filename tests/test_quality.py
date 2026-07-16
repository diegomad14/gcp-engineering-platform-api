"""Tests for normalized quality report ingestion and deployment evidence."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from eng_platform_api.main import app

client = TestClient(app)


def _payload(**overrides):
    return {
        "service_name": "test-api",
        "repository": "test-org/test-repo",
        "commit_sha": "a" * 40,
        "branch": "main",
        "profile": "python",
        "workflow_run_url": "https://github.com/test-org/test-repo/actions/runs/1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "coverage": 70.0,
        "coverage_threshold": 70.0,
        "tool_versions": {"ruff": "0.12.0"},
        "checks": [
            {
                "name": "Tests and coverage",
                "category": "tests",
                "status": "PASSED",
                "findings": 0,
                "blocking_findings": 0,
            },
            {
                "name": "Semgrep SAST",
                "category": "sast",
                "status": "PASSED",
                "findings": 0,
                "blocking_findings": 0,
            },
        ],
        **overrides,
    }


def test_quality_ingestion_requires_token(tmp_path, monkeypatch):
    monkeypatch.setenv("ENG_PLATFORM_QUALITY_STORE_PATH", str(tmp_path))
    monkeypatch.setenv("ENG_PLATFORM_QUALITY_INGEST_TOKEN", "quality-secret")

    response = client.post("/api/quality/reports", json=_payload())
    assert response.status_code == 401


def test_quality_report_is_idempotent_and_queryable(tmp_path, monkeypatch):
    monkeypatch.setenv("ENG_PLATFORM_QUALITY_STORE_PATH", str(tmp_path))
    monkeypatch.setenv("ENG_PLATFORM_QUALITY_INGEST_TOKEN", "quality-secret")
    headers = {"Authorization": "Bearer quality-secret"}

    first = client.post("/api/quality/reports", json=_payload(), headers=headers)
    second = client.post("/api/quality/reports", json=_payload(), headers=headers)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["quality_gate_status"] == "PASSED"

    evidence = client.get(f"/api/quality/services/test-api/commits/{'a' * 40}")
    assert evidence.status_code == 200
    assert evidence.json()["quality_gate_status"] == "PASSED"

    reports = list(tmp_path.glob("quality/reports/test-api/*.json"))
    assert len(reports) == 1
    history = client.get("/api/quality/services/test-api/reports")
    assert history.status_code == 200
    assert len(history.json()) == 1


def test_coverage_below_threshold_fails_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("ENG_PLATFORM_QUALITY_STORE_PATH", str(tmp_path))
    monkeypatch.setenv("ENG_PLATFORM_QUALITY_INGEST_TOKEN", "quality-secret")
    response = client.post(
        "/api/quality/reports",
        json=_payload(coverage=69.9),
        headers={"Authorization": "Bearer quality-secret"},
    )
    assert response.status_code == 201
    assert response.json()["quality_gate_status"] == "FAILED"


def test_summary_includes_unconfigured_catalog_services(tmp_path, monkeypatch):
    monkeypatch.setenv("ENG_PLATFORM_QUALITY_STORE_PATH", str(tmp_path))
    response = client.get("/api/quality/summary")
    assert response.status_code == 200
    projects = response.json()["projects"]
    assert projects
    assert all("service_name" in project for project in projects)
    assert any(
        project["quality_gate_status"] == "NOT_CONFIGURED" for project in projects
    )
