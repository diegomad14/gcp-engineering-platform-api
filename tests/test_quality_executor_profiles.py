from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import os
import subprocess
import sys

import pytest


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
        ],
        cwd=Path(__file__).resolve().parents[1] / "docker/quality-executor",
        check=True,
        capture_output=True,
        text=True,
    )


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
