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
