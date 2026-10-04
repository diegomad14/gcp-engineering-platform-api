"""Catalog authority and ACL are local, complete, strict and fail closed."""

from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from eng_platform_api.config import config, load_config
from eng_platform_api.main import app
from eng_platform_api.models import ServiceDetail
from eng_platform_api.services import catalog, log_catalog


# Fully synthetic private-authority fixtures; no operational identity or ACL.
SYNTHETIC_PRIVATE_RESOURCES = {
    "cloud_run_service": {
        f"demo-private-service-{index:02d}" for index in range(1, 23)
    },
    "cloud_run_job": {f"demo-private-job-{index:02d}" for index in range(1, 30)},
}
SYNTHETIC_PRIVATE_NAMES = sorted(set().union(*SYNTHETIC_PRIVATE_RESOURCES.values()))


def record(name="future-api", **changes):
    return {
        "service_name": name,
        "repository": "example/future",
        "owner": "platform",
        "project_id": "example-project",
        "region": "us-central1",
        "deployment": {"runtime_kind": "cloud_run_service", "enabled": False},
        **changes,
    }


def install_catalog(monkeypatch, tmp_path, rows):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"services": rows}))
    monkeypatch.setattr(log_catalog, "CATALOG_PATH", path)
    return path


def synthetic_private_catalog():
    rows = []
    for kind, names in SYNTHETIC_PRIVATE_RESOURCES.items():
        managed_count = 15 if kind == "cloud_run_service" else 4
        for index, name in enumerate(sorted(names)):
            row = record(
                name,
                management_mode="managed",
                repository="demo/example-repository",
                owner="demo-team",
                project_id="demo-platform-prod",
                deployment={"enabled": False, "runtime_kind": kind},
                logs={"enabled": True, "allowed_logins": ["demo-reader"]},
            )
            if index >= managed_count:
                row.update(
                    management_mode="observability_only",
                    repository=None,
                    owner=None,
                    environment="",
                    release_model="observability-only",
                    release_policy="",
                    quality={"enabled": False},
                    operational_secrets=[],
                    inventory_source={
                        "source": "cloud_run_inventory",
                        "observed_at": "2020-01-01T00:00:00+00:00",
                        "project": "demo-platform-prod",
                        "region": "us-central1",
                    },
                )
            rows.append(row)
    return rows


def install_synthetic_private_catalog(monkeypatch, tmp_path):
    path = install_catalog(monkeypatch, tmp_path, synthetic_private_catalog())
    pin_private_catalog(monkeypatch, path)
    return path


def pin_private_catalog(monkeypatch, path):
    monkeypatch.setattr(config, "catalog_path", str(path))
    monkeypatch.setattr(
        config, "catalog_sha256", hashlib.sha256(path.read_bytes()).hexdigest()
    )


def test_public_catalog_has_explicit_denied_policy_and_synthetic_inventory():
    entries = log_catalog.load_catalog()
    assert len(entries) == 51
    assert all(r["logs"] == {"enabled": False, "allowed_logins": []} for r in entries)
    observed = [r for r in entries if r["management_mode"] == "observability_only"]
    assert {r["service_name"] for r in observed} == {
        f"demo-observed-{kind}-{index:02d}"
        for kind, count in (("service", 7), ("job", 25))
        for index in range(1, count + 1)
    }
    assert all(r["project_id"] == "demo-platform-prod" for r in observed)
    assert not any(r.can_read("demo-reader") for r in log_catalog.resources())


def test_synthetic_private_catalog_has_exact_approved_policy_and_runtime(
    monkeypatch, tmp_path
):
    path = install_synthetic_private_catalog(monkeypatch, tmp_path)
    raw = json.loads(path.read_text())["services"]
    entries = log_catalog.load_catalog()
    assert raw == entries
    assert len(entries) == 51
    assert all(
        r["logs"] == {"enabled": True, "allowed_logins": ["demo-reader"]}
        for r in entries
    )
    resources = log_catalog.resources()
    assert {r.key for r in resources} == {
        ("demo-platform-prod", "us-central1", kind, name)
        for kind, names in SYNTHETIC_PRIVATE_RESOURCES.items()
        for name in names
    }
    assert len(resources) == len(SYNTHETIC_PRIVATE_NAMES) == 51
    assert all(r.can_read("demo-reader") for r in resources)
    assert all(r.can_read("Demo-Reader") for r in resources)
    for login in ("reader", "other", "demo-reader-other", "demo-reader ", "", "*"):
        assert not any(r.can_read(login) for r in resources)


def test_synthetic_approval_does_not_enable_global_defaults(monkeypatch, tmp_path):
    install_synthetic_private_catalog(monkeypatch, tmp_path)
    monkeypatch.delenv("ENG_PLATFORM_LOGS_ENABLED", raising=False)
    monkeypatch.delenv("ENG_PLATFORM_LOGS_ALLOWED_GITHUB_LOGINS", raising=False)
    defaults = load_config().logs
    assert defaults.enabled is False
    assert defaults.allowed_logins == ()


def test_current_public_catalog_flags_do_not_expose_reader_lists():
    response = TestClient(app).get("/api/catalog/services")
    assert response.status_code == 200
    entries = response.json()["services"]
    assert len(entries) == 51
    assert all(r["logs"] == {"enabled": False, "configured": True} for r in entries)
    assert "allowed_logins" not in response.text


@pytest.mark.parametrize("long_names_and_projects", [False, True])
def test_500_new_resources_and_multiple_projects_have_no_name_list(
    monkeypatch, tmp_path, long_names_and_projects
):
    def name(index):
        return (
            f"f{index:04d}-" + "x" * 57
            if long_names_and_projects
            else f"future-{index}"
        )

    rows = [
        record(
            name(index),
            project_id=f"project-{index if long_names_and_projects else index % 5}",
            region="us-central1" if index % 2 else "europe-west1",
            deployment={
                "runtime_kind": "cloud_run_job" if index % 3 else "cloud_run_service"
            },
        )
        for index in range(500)
    ]
    install_catalog(monkeypatch, tmp_path, rows)
    resources = log_catalog.resources()
    assert len(resources) == 500
    assert all(not r.enabled and r.allowed_logins == () for r in resources)
    assert not any(r.can_read("demo-reader") for r in resources)
    assert catalog.get_services().total == 500
    expected = "project-499" if long_names_and_projects else "project-4"
    assert catalog.get_service(name(499)).project_id == expected


def test_logs_omitted_empty_and_disabled_never_grant(monkeypatch, tmp_path):
    rows = [
        record("missing-policy"),
        record("empty-readers", logs={"enabled": True, "allowed_logins": []}),
        record(
            "disabled-reader", logs={"enabled": False, "allowed_logins": ["reader"]}
        ),
    ]
    install_catalog(monkeypatch, tmp_path, rows)
    assert not any(r.can_read("reader") for r in log_catalog.resources())
    assert not catalog.get_service("missing-policy").logs.configured
    assert catalog.get_service("empty-readers").logs.configured


@pytest.mark.parametrize(
    "policy",
    [
        None,
        [],
        "true",
        {},
        {"enabled": True},
        {"allowed_logins": ["reader"]},
        {"enabled": "false", "allowed_logins": ["reader"]},
        {"enabled": 1, "allowed_logins": ["reader"]},
        {"enabled": False, "allowed_logins": "reader"},
        {"enabled": True, "allowed_logins": None},
        {"enabled": True, "allowed_logins": ["*"]},
        {"enabled": True, "allowed_logins": ["reader@example.com"]},
        {"enabled": True, "allowed_logins": ["reader "]},
        {"enabled": True, "allowed_logins": ["reader", "Reader"]},
        {"enabled": True, "allowed_logins": ["reader"], "global": True},
    ],
)
def test_malformed_policy_fails_closed(policy):
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.validate_catalog({"services": [record(logs=policy)]})


def test_reader_case_normalization_fingerprint_and_private_http(monkeypatch, tmp_path):
    install_catalog(
        monkeypatch,
        tmp_path,
        [record(logs={"enabled": True, "allowed_logins": ["ReAdEr"]})],
    )
    resource = log_catalog.resources()[0]
    assert resource.can_read("READER")
    assert not resource.can_read("reader ")
    assert not resource.can_read(3)
    assert resource.key == (
        "example-project",
        "us-central1",
        "cloud_run_service",
        "future-api",
    )
    assert resource.name_label == "service_name"
    assert resource.resource_type == "cloud_run_revision"
    assert len(resource.policy_fingerprint) == 64
    for changed in (
        replace(resource, enabled=False),
        replace(resource, allowed_logins=("other",)),
        replace(resource, region="europe-west1"),
        replace(resource, kind="cloud_run_job"),
    ):
        assert resource.policy_fingerprint != changed.policy_fingerprint
    job = replace(resource, kind="cloud_run_job")
    assert job.resource_type == "cloud_run_job" and job.name_label == "job_name"
    payload = TestClient(app).get("/api/catalog/services").json()
    assert payload["services"][0]["logs"] == {"enabled": True, "configured": True}
    assert "reader" not in json.dumps(payload).lower()
    assert "allowed_logins" not in json.dumps(payload)


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"region": "europe-west1"},
        {"project_id": "another-project"},
        {"deployment": {"runtime_kind": "cloud_run_job"}},
    ],
)
def test_duplicate_names_rejected_across_every_coordinate(changes):
    with pytest.raises(log_catalog.CatalogUnavailable, match="Duplicate"):
        log_catalog.validate_catalog({"services": [record(), record(**changes)]})


@pytest.mark.parametrize("field", ["project_id", "region", "deployment"])
def test_coordinates_must_be_explicit(field):
    entry = record()
    del entry[field]
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.validate_catalog({"services": [entry]})
    if field == "deployment":
        entry[field] = {"enabled": True}
        with pytest.raises(log_catalog.CatalogUnavailable):
            log_catalog.validate_catalog({"services": [entry]})


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {},
        {"services": []},
        {"services": {}},
        {"services": [None]},
        {"services": [record(region="us-central1\n")]},
        {"services": [record()], "other": True},
    ],
)
def test_invalid_root_or_entries_are_never_partial(data):
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.validate_catalog(data)


@pytest.mark.parametrize(
    "raw", [None, b"{", b"\xff", b'{"services":[],"services":[]}', b'{"services":NaN}']
)
def test_missing_corrupt_catalog_sanitized_at_http(monkeypatch, tmp_path, raw):
    path = tmp_path / "sensitive-filename.json"
    if raw is not None:
        path.write_bytes(raw)
    monkeypatch.setattr(log_catalog, "CATALOG_PATH", path)
    with pytest.raises(
        log_catalog.CatalogUnavailable, match="^Runtime catalog is unavailable$"
    ):
        log_catalog.resources()
    for url in ("/api/catalog/services", "/api/catalog/services/unknown"):
        response = TestClient(app).get(url)
        assert response.status_code == 503
        assert response.json() == {"detail": "Runtime catalog is unavailable"}
        assert "sensitive-filename" not in response.text


def test_finite_file_entry_and_project_limits(monkeypatch, tmp_path):
    path = install_catalog(monkeypatch, tmp_path, [record()])
    monkeypatch.setattr(log_catalog, "MAX_CATALOG_BYTES", 5)
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.load_catalog(path)
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.validate_catalog(
            {"services": [record(f"resource-{i}") for i in range(2049)]}
        )
    monkeypatch.setattr(log_catalog, "MAX_PROJECTS", 5)
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.validate_catalog(
            {
                "services": [
                    record(f"resource-{i}", project_id=f"project-{i}") for i in range(6)
                ]
            }
        )


def test_log_resolution_never_discovers_or_checks_readiness(monkeypatch):
    denied = Mock(side_effect=AssertionError("Cloud discovery is forbidden"))
    monkeypatch.setattr(catalog, "_run_client", denied)
    monkeypatch.setattr(catalog, "_job_client", denied)
    monkeypatch.setattr(catalog, "get_services", denied)
    monkeypatch.setattr(catalog, "deployment_blockers", denied)
    assert len(log_catalog.resources()) == 51
    denied.assert_not_called()


def test_public_detail_cache_does_not_keep_old_coordinates_or_capability(
    monkeypatch, tmp_path
):
    old = record(logs={"enabled": True, "allowed_logins": ["reader"]})
    path = install_catalog(monkeypatch, tmp_path, [old])
    pin_private_catalog(monkeypatch, path)
    previous = catalog.get_service("future-api")
    cached = ServiceDetail(
        **previous.model_dump(), url="https://old.example", status="healthy"
    )
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(
        catalog, "_detail_cache", {"future-api": (catalog.monotonic(), cached)}
    )
    client = Mock()
    client.get_service.side_effect = RuntimeError("not deployed")
    monkeypatch.setattr(catalog, "_run_client", lambda: client)
    updated = deepcopy(old)
    updated["region"] = "europe-west1"
    updated["logs"] = {"enabled": False, "allowed_logins": []}
    path.write_text(json.dumps({"services": [updated]}))
    pin_private_catalog(monkeypatch, path)
    detail = catalog.get_service_detail("future-api")
    assert detail.region == "europe-west1"
    assert detail.logs.enabled is False
    assert detail.url != "https://old.example"
    client.get_service.assert_called_once()


def test_public_detail_cache_retains_compatible_runtime_state(monkeypatch, tmp_path):
    path = install_catalog(monkeypatch, tmp_path, [record()])
    pin_private_catalog(monkeypatch, path)
    base = catalog.get_service("future-api")
    cached = ServiceDetail(**base.model_dump(), url="https://same.example")
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(
        catalog, "_detail_cache", {"future-api": (catalog.monotonic(), cached)}
    )
    denied = Mock(side_effect=AssertionError("Cache should avoid discovery"))
    monkeypatch.setattr(catalog, "_run_client", denied)
    assert catalog.get_service_detail("future-api").url == "https://same.example"
    denied.assert_not_called()


def test_checked_in_schema_matches_validation_model():
    schema = json.loads(
        (
            Path(__file__).resolve().parents[1] / "schemas/platform-catalog.schema.json"
        ).read_text()
    )
    generated = log_catalog.CatalogDocument.model_json_schema()
    for key in ("$defs", "properties", "required", "additionalProperties"):
        assert schema[key] == generated[key]


def test_real_catalog_also_validates_published_json_schema():
    from jsonschema import Draft202012Validator

    schema = json.loads(
        (
            Path(__file__).resolve().parents[1] / "schemas/platform-catalog.schema.json"
        ).read_text()
    )
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(
        json.loads(log_catalog.CATALOG_PATH.read_text())
    )
