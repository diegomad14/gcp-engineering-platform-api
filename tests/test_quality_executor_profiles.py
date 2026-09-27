from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import os
import subprocess

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
