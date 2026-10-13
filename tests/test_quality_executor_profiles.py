from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import json
import os
import shlex
import subprocess
import sys
import tomllib

import pytest
import yaml


def _executor_profiles():
    path = (
        Path(__file__).resolve().parents[1]
        / "docker/quality-executor/quality_profiles.py"
    )
    spec = spec_from_file_location("quality_executor_profiles", path)
    assert spec is not None and spec.loader is not None
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_artemis_quality_aliases_preserve_profile_and_hash():
    profiles = _executor_profiles()

    assert "cgm-artemis-api" in profiles.available_services()
    assert "cgm-artemis-web" in profiles.available_services()
    assert profiles.profile_for("cgm-artemis-api") == profiles.profile_for(
        "cgm-sanplat-api"
    )
    assert profiles.profile_for("cgm-artemis-web") == profiles.profile_for(
        "cgm-sanplat-web"
    )
    assert profiles.profile_hash("cgm-artemis-api") == profiles.profile_hash(
        "cgm-sanplat-api"
    )
    assert profiles.profile_hash("cgm-artemis-web") == profiles.profile_hash(
        "cgm-sanplat-web"
    )


def test_image_profile_contract_is_also_checked_by_api_ci():
    subprocess.run(
        [
            sys.executable,
            "-m",
            "unittest",
            "test_quality_executor.QualityProfilesTest.test_profiles_preserve_security_and_coverage_contracts",
            "test_quality_executor.QualityIsolationTest.test_isolated_checkout_with_simulated_different_owner",
        ],
        cwd=Path(__file__).resolve().parents[1] / "docker/quality-executor",
        check=True,
        capture_output=True,
        text=True,
    )


def test_api_quality_commands_instrument_the_catalog_registry():
    """The pinned profile and both native CI commands retain every source."""
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text())
    addopts = shlex.split(project["tool"]["pytest"]["ini_options"]["addopts"])
    assert "--cov=scripts.catalog_registry" in addopts
    assert any(
        dependency.startswith("pytest-cov")
        for dependency in project["project"]["optional-dependencies"]["dev"]
    )
    sources = json.loads((root / ".quality-sources.json").read_text())
    assert "scripts/catalog_registry.py" in sources["roots"]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    native = next(
        line.strip()
        for step in workflow["jobs"]["quality"]["steps"]
        for line in step.get("run", "").splitlines()
        if line.strip().startswith("pytest ")
    )
    commands = (
        native,
        workflow["jobs"]["normalized"]["with"]["test-command"],
        _executor_profiles().profile_for("eng-platform-api")["commands"]["tests"],
    )
    for command in commands:
        options = [*addopts, *shlex.split(command)]
        assert "--cov-reset" not in options
        assert "--no-cov" not in options
        assert {
            "--cov=eng_platform_api",
            "--cov=scripts.release",
            "--cov=scripts.catalog_registry",
        }.issubset(options)


def test_smarti_extras_are_mandatory_and_change_both_alias_contracts():
    profiles = _executor_profiles()
    web = profiles.profile_for("cgm-artemis-web")
    api = profiles.profile_for("cgm-artemis-api")
    ux = next(extra for extra in web["extra"] if extra["category"] == "smarti_ux")
    pg = next(extra for extra in api["extra"] if extra["category"] == "smarti_postgres")
    assert ux["blocking"] is True
    assert pg["blocking"] is True
    assert ux["command"] == "python3 /opt/eng-platform/smarti_ux.py"
    assert pg["command"] == "python /opt/eng-platform/smarti_pg.py"
    assert web["coverage_threshold"] == api["coverage_threshold"] == 70
    assert web["timeout_seconds"] == api["timeout_seconds"] == 3600
    assert any(extra["category"] == "proxy_contracts" for extra in web["extra"])
    assert (
        profiles.profile_hash("cgm-artemis-web")
        != "a61b1e71e8328993031c55ed19067085be6b1dea93c3d2ad5bafda46d3fbdbf5"
    )
    assert profiles.profile_hash("cgm-artemis-api") != profiles.profile_hash(
        "communications-ms"
    )
    profiles.verify_profile_hash(
        "cgm-artemis-web", profiles.profile_hash("cgm-sanplat-web")
    )
    profiles.verify_profile_hash(
        "cgm-artemis-api", profiles.profile_hash("cgm-sanplat-api")
    )


@pytest.mark.parametrize("formatted", [True, False])
def test_bot_format_command_uses_service_relative_paths(tmp_path, formatted):
    """Exercise the actual profile command from its nested working directory."""
    profile = _executor_profiles().profile_for("cgm-bot-api")
    repo = tmp_path / "repository"
    service = repo / profile["working_directory"]
    app = service / "app" / "module"
    app.mkdir(parents=True)
    source = app / "with space.py"
    source.write_text("value = 1\n")

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.invalid")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    source.write_text("value = 2\n" if formatted else "value=  2\n")
    git("add", ".")
    git("commit", "-qm", "change")
    result = subprocess.run(
        ["bash", "-o", "pipefail", "-c", profile["commands"]["format"]],
        cwd=service,
        env={
            **os.environ,
            "ENG_PLATFORM_RELEASE_BASE_SHA": base,
            "ENG_PLATFORM_RELEASE_HEAD_SHA": git("rev-parse", "HEAD"),
        },
        text=True,
        capture_output=True,
    )
    assert "No such file" not in result.stderr
    if formatted:
        assert result.returncode == 0, result.stderr
        assert "1 file already formatted" in result.stdout
    else:
        assert result.returncode != 0
        assert "Would reformat: app/module/with space.py" in result.stdout


@pytest.mark.parametrize("coverage", [None, 0, 80, 91.25])
@pytest.mark.parametrize("threshold", [None, 80, 80.0])
def test_sealed_executor_report_hash_matches_api_numeric_serialization(
    monkeypatch, coverage, threshold
):
    from eng_platform_api.models import QualityReportCreate
    from eng_platform_api.services.quality_store import _report_hash
    import importlib

    monkeypatch.syspath_prepend(
        str(Path(__file__).resolve().parents[1] / "docker/quality-executor")
    )
    engine = importlib.import_module("quality_executor")
    report = {
        "service_name": "eng-platform-api",
        "repository": "owner/repository",
        "commit_sha": "a" * 40,
        "profile": "python",
        "generated_at": "2026-10-13T00:00:00Z",
        "coverage": coverage,
        "coverage_threshold": threshold,
        "differential_coverage": coverage,
        "differential_threshold": threshold,
        "checks": [
            {
                "name": "tests",
                "category": "tests",
                "status": "PASSED",
                "duration_seconds": 0,
            }
        ],
    }
    normalized = engine._normalize_report(report)
    assert normalized == QualityReportCreate.model_validate(normalized).model_dump(
        mode="json"
    )
    assert engine._report_hash(report) == _report_hash(
        QualityReportCreate.model_validate(report)
    )
