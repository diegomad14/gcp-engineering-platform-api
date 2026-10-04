"""The public CLI must produce the same mandatory gate as the API."""

import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


@pytest.mark.parametrize("runtime", ["python", "node", "static"])
def test_cli_generates_oss_onboarding(tmp_path, runtime):
    result = subprocess.run(
        [
            sys.executable,
            "scripts/service_factory.py",
            "--app-name",
            "example",
            "--service-name",
            "example-api",
            "--service-type",
            "api",
            "--runtime",
            runtime,
            "--gcp-project",
            "test-project",
            "--owner",
            "platform",
            "--repository",
            "example/example",
            "--cost-center",
            "engineering",
            "--working-directory",
            "backend",
            "--coverage-threshold",
            "80",
            "--sonar-project-key",
            "legacy-ignored",
            "--sonar-organization",
            "legacy-ignored",
            "--output-dir",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert "Generated 11 files" in result.stdout
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert len(files) == 11
    assert all("sonar" not in path.name.lower() for path in files)
    assert all("SONAR_TOKEN" not in path.read_text() for path in files)
    contract = yaml.safe_load((tmp_path / "gcp-service-release.yaml").read_text())
    gate = contract["quality"]["quality_gate"]
    assert gate["policy_version"] == "oss-v2"
    assert gate["coverage_threshold"] == 80
    assert gate["differential_threshold"] == 80
    assert gate["blocking"] is True
    assert contract["service"]["repository"] == "example/example"
    assert json.loads((tmp_path / "backend/.quality-sources.json").read_text())[
        "roots"
    ] == ["src"]
    ci = (tmp_path / ".github/workflows/ci.yml").read_text()
    assert "reusable-quality-gate.yml@" in ci
    assert "working-directory: backend" in ci
    for workflow in (
        "platform-deploy.yml",
        "platform-rollback.yml",
        "semantic-release.yml",
    ):
        assert (tmp_path / ".github/workflows" / workflow).is_file()


def test_cli_job_proposal_has_no_service_deploy_workflow(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "scripts/service_factory.py",
            "--app-name",
            "batch",
            "--service-name",
            "example-batch-job",
            "--service-type",
            "worker",
            "--runtime",
            "python",
            "--runtime-kind",
            "cloud_run_job",
            "--gcp-project",
            "test-project",
            "--owner",
            "platform",
            "--cost-center",
            "engineering",
            "--output-dir",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    assert "Generated 9 files" in result.stdout
    assert "No GCP resources were created" in result.stdout
    assert (tmp_path / "gcp-job-release.yaml").exists()
    assert (tmp_path / "cloud-run-job-labels.yaml").exists()
    assert not (tmp_path / ".github/workflows/platform-deploy.yml").exists()
    assert not (tmp_path / ".github/workflows/platform-rollback.yml").exists()
    from eng_platform_api.services.log_catalog import validate_catalog

    entry = yaml.safe_load(
        (tmp_path / "catalog/services/example-batch-job.yaml").read_text()
    )
    assert (
        validate_catalog({"services": [entry]})[0]["deployment"]["runtime_kind"]
        == "cloud_run_job"
    )
    assert entry["deployment"]["enabled"] is False
    assert entry["logs"] == {"enabled": False, "allowed_logins": []}


@pytest.mark.parametrize(
    "kind,expected_count", [("cloud_run_service", 11), ("cloud_run_job", 9)]
)
def test_cli_main_defaults_to_local_proposal_directory(
    tmp_path, monkeypatch, capsys, kind, expected_count
):
    from scripts import service_factory

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SERVICE_FACTORY_OUTPUT", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "service_factory.py",
            "--app-name",
            "example",
            "--service-name",
            "new-runtime",
            "--service-type",
            "worker",
            "--runtime",
            "python",
            "--runtime-kind",
            kind,
            "--gcp-project",
            "test-project",
            "--owner",
            "platform",
            "--cost-center",
            "ops",
            "--validation-targets",
            " external-a, external-b, ",
        ],
    )
    service_factory.main()
    assert not (tmp_path / ".github").exists()
    output = tmp_path / "service-factory-proposal"
    assert len([path for path in output.rglob("*") if path.is_file()]) == expected_count
    assert f"Generated {expected_count} files" in capsys.readouterr().out
    entry = yaml.safe_load((output / "catalog/services/new-runtime.yaml").read_text())
    assert entry["repository"] == "platform/example"
    assert entry["logs"] == {"enabled": False, "allowed_logins": []}
