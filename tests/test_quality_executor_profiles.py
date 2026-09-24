from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


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
