"""Go onboarding requires exact OSS evidence and keeps existing runner policy."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
import yaml

from eng_platform_api.config import config, load_config
from eng_platform_api.models import CatalogService
from eng_platform_api.services import github_actions_quota as quota
from eng_platform_api.services import release_orchestrator as orchestrator
from eng_platform_api.services import quality_profiles, release_profiles
from eng_platform_api.services.log_catalog import validate_catalog

ROOT = Path(__file__).parents[1]


@pytest.fixture
def go_coverage(monkeypatch):
    path = ROOT / "scripts/quality/go_coverage.py"
    spec = importlib.util.spec_from_file_location("native_go_coverage", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def source(tmp_path):
    (tmp_path / "internal").mkdir()
    code = tmp_path / "internal/service.go"
    code.write_text("package service\nfunc Check() {\n x := 1\n _ = x\n}\n")
    (tmp_path / "go.mod").write_text(
        "module github.com/example/reconnections\ngo 1.27.2\n"
    )
    (tmp_path / ".quality-sources.json").write_text(
        json.dumps({"roots": ["internal"], "exclude": ["*_test.go"]})
    )
    return code


def test_go_native_statement_and_line_evidence(go_coverage, tmp_path, monkeypatch):
    code = source(tmp_path)
    report = tmp_path / "coverage.out"
    report.write_text(
        "mode: atomic\ngithub.com/example/reconnections/internal/service.go:3.2,4.2 1 1\ngithub.com/example/reconnections/internal/service.go:4.2,4.7 1 0\n"
    )
    monkeypatch.setattr(
        go_coverage.subprocess,
        "check_output",
        lambda *_args, **_kw: json.dumps({str(code): [[3, 2], [4, 2]]}),
    )
    percentage, lines = go_coverage.go_coverage(report, tmp_path, tmp_path)
    assert percentage == 50
    assert lines == {"internal/service.go": {3: True, 4: False}}


@pytest.mark.parametrize(
    "evidence,error",
    [
        ("mode: bogus\n", "mode"),
        ("mode: atomic\ninvalid\n", "Malformed"),
        ("mode: atomic\n../outside.go:3.2,4.2 1 1\n", "subpath"),
        (
            "mode: atomic\ngithub.com/example/reconnections/internal/service.go:3.2,3.8 1 1\n",
            "missing from coverage",
        ),
    ],
)
def test_go_missing_malformed_or_outside_evidence_fails(
    go_coverage, tmp_path, monkeypatch, evidence, error
):
    code = source(tmp_path)
    report = tmp_path / "coverage.out"
    report.write_text(evidence)
    monkeypatch.setattr(
        go_coverage.subprocess,
        "check_output",
        lambda *_args, **_kw: json.dumps({str(code): [[3, 2], [4, 2]]}),
    )
    with pytest.raises(ValueError, match=error):
        go_coverage.go_coverage(report, tmp_path, tmp_path)


def test_go_catalog_proposal_and_server_owned_profiles_agree(monkeypatch):
    entry = yaml.safe_load(
        (ROOT / "catalog/services/cgm-reconnections-api.yaml").read_text()
    )
    validated = validate_catalog({"services": [entry]})
    assert validated
    entry.pop("logs")
    service = CatalogService(**entry)
    profile = quality_profiles.profile_for(service)
    assert profile.runtime == "go" and profile.coverage_threshold == 80
    assert "-coverpkg=./..." in profile.spec["commands"]["tests"]
    assert "-race" in profile.spec["commands"]["tests"]
    assert release_profiles.profile_for(service).name == service.service_name
    monkeypatch.setattr(
        config.release_orchestrator,
        "quality_go_image",
        "registry.example/quality-go@sha256:" + "a" * 64,
    )
    assert "quality-go@sha256:" in quality_profiles.executor_image(profile)
    monkeypatch.setattr(
        config.release_orchestrator,
        "quality_go_image",
        "registry.example/quality-go:latest",
    )
    with pytest.raises(ValueError, match="digest"):
        quality_profiles.executor_image(profile)


def test_go_optional_pin_preserves_existing_controller_startup():
    with mock.patch.dict("os.environ", {"ENG_PLATFORM_MOCK_MODE": "true"}, clear=True):
        assert load_config().release_orchestrator.quality_go_image == ""
    with mock.patch.dict(
        "os.environ",
        {
            "ENG_PLATFORM_MOCK_MODE": "true",
            "ENG_PLATFORM_QUALITY_GO_IMAGE": "image:latest",
        },
        clear=True,
    ):
        with pytest.raises(ValueError, match="Go quality"):
            load_config()


@pytest.mark.parametrize(
    "opened,expected", [(False, "github_actions"), (True, "cloud_build")]
)
def test_github_first_exception_preserves_billing_fallback(
    monkeypatch, opened, expected
):
    service = CatalogService(
        service_name="cgm-reconnections-api",
        repository="owner/private",
        owner="cgm",
        project_id="test-project",
        region="us-central1",
    )
    monkeypatch.setattr(
        config.release_orchestrator, "private_executor_mode", "cloud_build"
    )
    monkeypatch.setattr(
        config.release_orchestrator, "github_first_services", (service.service_name,)
    )
    monkeypatch.setattr(
        orchestrator.github_release_control, "repository_is_private", lambda _: True
    )
    monkeypatch.setattr(
        orchestrator.executor_circuits,
        "get",
        lambda _: {"state": "open" if opened else "closed"},
    )
    monkeypatch.setattr(
        orchestrator.executor_circuits, "require_billing_rejection", lambda _: None
    )
    assert orchestrator._provider(service) == expected
    service.service_name = "existing-private"
    assert orchestrator._provider(service) == "cloud_build"


def test_github_first_deployment_keeps_explicit_restrictions(monkeypatch):
    name = "cgm-reconnections-api"
    monkeypatch.setattr(
        config.release_orchestrator, "private_executor_mode", "cloud_build"
    )
    monkeypatch.setattr(config.release_orchestrator, "github_first_services", (name,))
    monkeypatch.setattr(config.cloud_build, "enabled", True)
    monkeypatch.setattr(config.cloud_build, "enabled_services", (name,))
    monkeypatch.setattr(config.cloud_build, "cloud_build_only_services", ())
    monkeypatch.setattr(config.cloud_build, "mode", "auto")
    monkeypatch.setattr(quota.catalog, "get_service", lambda _: None)
    monkeypatch.setattr(
        quota,
        "github_client",
        lambda: SimpleNamespace(get_repo=lambda _: SimpleNamespace(private=True)),
    )
    monkeypatch.setattr(quota.executor_circuits, "get", lambda _: {"state": "closed"})
    assert quota.should_use_cloud_build(name, "owner/private") is False
    monkeypatch.setattr(config.cloud_build, "cloud_build_only_services", (name,))
    assert quota.should_use_cloud_build(name, "owner/private") is True
    service = CatalogService(
        service_name=name,
        repository="owner/private",
        owner="cgm",
        project_id="test-project",
        region="us-central1",
    )
    monkeypatch.setattr(
        orchestrator.github_release_control, "repository_is_private", lambda _: True
    )
    assert orchestrator._provider(service) == "cloud_build"


def test_go_image_and_api_use_identical_additive_profile_hash():
    path = ROOT / "docker/quality-executor/quality_profiles.py"
    spec = importlib.util.spec_from_file_location("go_executor_profiles", path)
    profiles = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(profiles)
    entry = yaml.safe_load(
        (ROOT / "catalog/services/cgm-reconnections-api.yaml").read_text()
    )
    entry.pop("logs")
    service = CatalogService(**entry)
    assert (
        profiles.profile_hash(service.service_name)
        == quality_profiles.profile_for(service).fingerprint()
    )
    assert profiles.profile_for(service.service_name)["commands"]["build"]
    assert profiles.profile_for(service.service_name)["commands"]["format"]


def test_go_extension_cannot_override_pinned_profiles(tmp_path, monkeypatch):
    path = ROOT / "docker/quality-executor/quality_profiles.py"
    spec = importlib.util.spec_from_file_location("go_executor_extension", path)
    profiles = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(profiles)
    primary = tmp_path / "release_quality_profiles.json"
    primary.write_bytes(
        (ROOT / "src/eng_platform_api/release_quality_profiles.json").read_bytes()
    )
    extension = tmp_path / "release_quality_profiles.go.json"
    extension.write_text(
        json.dumps({"schema_version": 1, "profiles": {"eng-platform-api": {}}})
    )
    monkeypatch.setenv("ENG_PLATFORM_RELEASE_PROFILE_PATH", str(primary))
    with pytest.raises(
        profiles.QualityProfileError, match="overrides a pinned profile"
    ):
        profiles.profile_document()
