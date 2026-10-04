"""Local proposal registration shares the exact runtime catalog authority."""

import json
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest
import yaml

from eng_platform_api.models import ServiceFactoryRequest
from eng_platform_api.services import catalog, log_catalog
from eng_platform_api.services.service_factory import generate_plan
from scripts import catalog_registry as registry


ROOT = Path(__file__).resolve().parents[1]


def entry(name="new-resource", kind="cloud_run_service", project="test-project"):
    request = ServiceFactoryRequest(
        repository="test-org/test-repository",
        service_name=name,
        service_type="worker",
        runtime="python",
        runtime_kind=kind,
        gcp_project=project,
        owner="test-team",
    )
    return yaml.safe_load(generate_plan(request).catalog_entry)


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "runtime-catalog.json"
    original = entry("existing-resource")
    original["logs"] = {"enabled": True, "allowed_logins": ["Existing-Reader"]}
    path.write_text(json.dumps({"services": [original]}))
    return path


def proposal(tmp_path, value=None):
    path = tmp_path / "proposal.yaml"
    path.write_text(yaml.safe_dump(entry() if value is None else value))
    return path


@pytest.mark.parametrize("kind", ["cloud_run_service", "cloud_run_job"])
def test_write_registers_shared_authority_without_code_changes(
    tmp_path, source, monkeypatch, kind
):
    path = proposal(tmp_path, entry(kind=kind))
    original = json.loads(source.read_text())["services"][0]
    assert registry.main(["--write", "--catalog", str(source), str(path)]) == 0
    saved = json.loads(source.read_text())
    assert saved["services"][0] == original
    registered = log_catalog.load_catalog(source)[1]
    assert registered["deployment"]["runtime_kind"] == kind
    assert registered["logs"] == {"enabled": False, "allowed_logins": []}
    monkeypatch.setattr(log_catalog, "CATALOG_PATH", source)
    assert "new-resource" in [
        item.service_name for item in catalog.get_services().services
    ]
    resource = next(
        item for item in log_catalog.resources() if item.service_id == "new-resource"
    )
    assert resource.kind == kind
    assert not resource.can_read("Existing-Reader")


@pytest.mark.parametrize("kind", ["cloud_run_service", "cloud_run_job"])
def test_new_factory_registration_does_not_inherit_current_catalog_approval(
    tmp_path, monkeypatch, kind
):
    from tests.test_log_catalog import (
        install_synthetic_private_catalog,
        pin_private_catalog,
    )

    install_synthetic_private_catalog(monkeypatch, tmp_path)
    original = log_catalog.load_catalog()
    assert len(original) == 51
    source = tmp_path / "runtime-catalog.json"
    source.write_bytes(log_catalog.CATALOG_PATH.read_bytes())
    value = entry(kind=kind)
    assert value["logs"] == {"enabled": False, "allowed_logins": []}
    path = proposal(tmp_path, value)
    assert registry.main(["--write", "--catalog", str(source), str(path)]) == 0
    saved = log_catalog.load_catalog(source)
    assert len(saved) == 52
    assert saved[:51] == original
    assert saved[51]["logs"] == {"enabled": False, "allowed_logins": []}
    monkeypatch.setattr(log_catalog, "CATALOG_PATH", source)
    pin_private_catalog(monkeypatch, source)
    resources = log_catalog.resources()
    assert all(resource.can_read("demo-reader") for resource in resources[:51])
    assert not resources[51].can_read("demo-reader")


@pytest.mark.parametrize("mode", [[], ["--check"], ["--write"]])
def test_cli_default_check_and_explicit_write(tmp_path, source, mode):
    path = proposal(tmp_path)
    before = source.read_bytes()
    result = subprocess.run(
        [
            sys.executable,
            "scripts/catalog_registry.py",
            *mode,
            "--catalog",
            str(source),
            str(path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Log access remains disabled" in result.stdout
    assert "total=2 services=2 jobs=0 projects=1 added=1" in result.stdout
    assert "Existing-Reader" not in result.stdout
    assert (source.read_bytes() == before) == (mode != ["--write"])
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "proposal.yaml",
        "runtime-catalog.json",
    ]


def test_default_catalog_path_is_runtime_source(tmp_path, source, monkeypatch):
    monkeypatch.setattr(log_catalog, "CATALOG_PATH", source)
    registry.register(proposal(tmp_path), write=True)
    assert log_catalog.load_catalog()[1]["service_name"] == "new-resource"


@pytest.mark.parametrize(
    "kind,project",
    [("cloud_run_service", "test-project"), ("cloud_run_job", "other-project")],
)
def test_duplicate_identity_never_replaced(tmp_path, source, kind, project):
    before = source.read_bytes()
    with pytest.raises(registry.RegistrationError, match="globally unique"):
        registry.register(
            proposal(tmp_path, entry("existing-resource", kind, project)),
            source,
            write=True,
        )
    assert source.read_bytes() == before


@pytest.mark.parametrize(
    "logs",
    [
        {"enabled": True, "allowed_logins": []},
        {"enabled": False, "allowed_logins": ["new-reader"]},
        {"enabled": "false", "allowed_logins": []},
        {"enabled": 0, "allowed_logins": []},
        {"enabled": False},
        {"allowed_logins": []},
        {"enabled": False, "allowed_logins": [], "admin": True},
        None,
        [],
    ],
)
def test_import_cannot_grant_logs_or_accept_malformed_policy(tmp_path, source, logs):
    before = source.read_bytes()
    value = entry()
    value["logs"] = logs
    with pytest.raises(registry.RegistrationError, match="cannot grant log access"):
        registry.register(proposal(tmp_path, value), source, write=True)
    assert source.read_bytes() == before


def test_missing_policy_is_explicitly_denied_on_registration(tmp_path, source):
    value = entry()
    del value["logs"]
    registry.register(proposal(tmp_path, value), source, write=True)
    assert log_catalog.load_catalog(source)[1]["logs"] == {
        "enabled": False,
        "allowed_logins": [],
    }


@pytest.mark.parametrize(
    "raw",
    [
        "service_name: first\nservice_name: second\n",
        "logs:\n  enabled: false\n  enabled: true\n  allowed_logins: []\n",
        "base: &base {}\nlogs: *base\n",
        "logs:\n  <<: {enabled: true}\n",
        "[]",
        "1: value",
        "---\n{}\n---\n{}",
        "name: [",
        "name: !!python/object:os.system {}",
        b"\xff",
    ],
)
def test_rejects_ambiguous_or_unsafe_yaml(tmp_path, source, raw):
    path = tmp_path / "bad.yaml"
    path.write_bytes(raw if isinstance(raw, bytes) else raw.encode())
    before = source.read_bytes()
    with pytest.raises(ValueError):
        registry.register(path, source, write=True)
    assert source.read_bytes() == before


@pytest.mark.parametrize(
    "raw",
    [
        "x" * (registry.MAX_PROPOSAL_BYTES + 1),
        "[" * 33 + "]" * 33,
        "- a\n" * 4097,
    ],
)
def test_rejects_oversized_or_complex_yaml(tmp_path, source, raw):
    path = tmp_path / "big.yaml"
    path.write_text(raw)
    with pytest.raises(registry.RegistrationError):
        registry.register(path, source)


@pytest.mark.parametrize("change", ["runtime_kind", "project_id", "region", "unknown"])
def test_final_catalog_validator_rejects_invalid_metadata(tmp_path, source, change):
    value = entry()
    if change == "runtime_kind":
        del value["deployment"]["runtime_kind"]
    elif change == "unknown":
        value["unknown"] = "field"
    else:
        del value[change]
    with pytest.raises(log_catalog.CatalogUnavailable):
        registry.register(proposal(tmp_path, value), source)


@pytest.mark.parametrize(
    "raw", ["not json", '{"services":[],"services":[]}', '{"services":[]}']
)
def test_rejects_invalid_existing_catalog(tmp_path, source, raw):
    source.write_text(raw)
    with pytest.raises(log_catalog.CatalogUnavailable):
        registry.register(proposal(tmp_path), source, write=True)
    assert source.read_text() == raw


def test_no_existing_catalog_is_not_treated_as_empty(tmp_path):
    with pytest.raises(registry.RegistrationError, match="existing regular file"):
        registry.register(proposal(tmp_path), tmp_path / "missing.json")


def test_symlink_catalog_cannot_be_replaced(tmp_path, source):
    link = tmp_path / "linked.json"
    link.symlink_to(source)
    with pytest.raises(registry.RegistrationError, match="symlink"):
        registry.register(proposal(tmp_path), link, write=True)
    assert link.is_symlink()


def test_successful_write_uses_atomic_replace_and_preserves_mode(tmp_path, source):
    source.chmod(0o640)
    before = source.read_bytes()
    replace = registry.os.replace
    with mock.patch.object(registry.os, "replace", wraps=replace) as wrapped:
        registry.register(proposal(tmp_path), source, write=True)
    temporary, destination = wrapped.call_args.args
    assert Path(temporary).parent == source.parent
    assert destination == source
    assert source.read_bytes() != before
    assert source.stat().st_mode & 0o777 == 0o640
    assert not Path(temporary).exists()


def test_failed_atomic_write_leaves_source_unchanged(tmp_path, source):
    before = source.read_bytes()
    with mock.patch.object(
        registry.os, "replace", side_effect=OSError("simulated failure")
    ):
        with pytest.raises(OSError, match="simulated failure"):
            registry.register(proposal(tmp_path), source, write=True)
    assert source.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_detects_concurrent_editor_and_preserves_its_change(tmp_path, source):
    path = proposal(tmp_path)
    changed = json.dumps({"services": [entry("other-resource")]})
    with mock.patch.object(
        registry.os, "fsync", side_effect=lambda _fd: source.write_text(changed)
    ):
        with pytest.raises(
            registry.RegistrationError, match="changed during registration"
        ):
            registry.register(path, source, write=True)
    assert source.read_text() == changed
    assert not list(tmp_path.glob("*.tmp"))


def test_catalog_limit_applies_to_result_and_input(tmp_path, source, monkeypatch):
    path = proposal(tmp_path)
    before = source.read_bytes()
    monkeypatch.setattr(log_catalog, "MAX_CATALOG_BYTES", len(before) + 1)
    with pytest.raises(registry.RegistrationError, match="Updated catalog exceeds"):
        registry.register(path, source)
    monkeypatch.setattr(log_catalog, "MAX_CATALOG_BYTES", len(before) - 1)
    with pytest.raises(registry.RegistrationError, match="Catalog exceeds"):
        registry.register(path, source)


def test_cli_reports_validation_failure_without_mutation(tmp_path, source, capsys):
    path = proposal(tmp_path, {"service_name": "missing-metadata"})
    before = source.read_bytes()
    with pytest.raises(SystemExit) as error:
        registry.main(["--write", "--catalog", str(source), str(path)])
    assert error.value.code == 2
    assert "Registration failed" in capsys.readouterr().err
    assert source.read_bytes() == before


def test_detects_editor_during_catalog_validation(tmp_path, source):
    path = proposal(tmp_path)
    changed = json.dumps({"services": [entry("other-resource")]})
    load = log_catalog.load_catalog

    def edit_after_read(catalog_path):
        values = load(catalog_path)
        source.write_text(changed)
        return values

    with mock.patch.object(log_catalog, "load_catalog", side_effect=edit_after_read):
        with pytest.raises(
            registry.RegistrationError, match="changed during validation"
        ):
            registry.register(path, source, write=True)
    assert source.read_text() == changed


def test_cooperating_writers_preserve_both_new_entries(tmp_path, source):
    from concurrent.futures import ThreadPoolExecutor

    proposals = []
    for name in ("resource-a", "resource-b"):
        path = tmp_path / f"{name}.yaml"
        path.write_text(yaml.safe_dump(entry(name)))
        proposals.append(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        summaries = list(
            pool.map(
                lambda path: registry.register(path, source, write=True), proposals
            )
        )
    assert sorted(summary.total for summary in summaries) == [2, 3]
    assert {item["service_name"] for item in log_catalog.load_catalog(source)} == {
        "existing-resource",
        "resource-a",
        "resource-b",
    }
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("kind", ["cloud_run_service", "cloud_run_job"])
def test_newly_registered_resource_cannot_invoke_log_provider(
    tmp_path, source, monkeypatch, kind
):
    from fastapi import HTTPException
    from eng_platform_api.models import ServiceLogsRequest
    from eng_platform_api.routers.logs import _read_logs
    from eng_platform_api.services import log_budget, runtime_logs

    registry.register(proposal(tmp_path, entry(kind=kind)), source, write=True)
    monkeypatch.setattr(log_catalog, "CATALOG_PATH", source)
    with mock.patch.object(runtime_logs, "read_logs") as provider:
        with pytest.raises(HTTPException) as error:
            _read_logs(
                "new-resource",
                ServiceLogsRequest(),
                log_budget.Deadline.after(10),
                "Existing-Reader",
            )
    assert error.value.status_code == 403
    provider.assert_not_called()
    public = catalog.get_service("new-resource").model_dump()
    assert public["logs"] == {"enabled": False, "configured": True}
    assert "allowed_logins" not in json.dumps(public)
