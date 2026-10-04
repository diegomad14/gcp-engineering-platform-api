"""Mounted authority is selected only by trusted, digest-pinned configuration."""

from base64 import b64encode
from copy import deepcopy
import hashlib
import json
import os
from unittest.mock import Mock

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
import pytest

from eng_platform_api.config import config, load_config
from eng_platform_api.main import app
from eng_platform_api.services import catalog, log_budget, log_catalog, runtime_logs


PUBLIC = {
    "services": [
        {
            "service_name": "public-example",
            "repository": "example/public",
            "owner": "example-team",
            "project_id": "example-project",
            "region": "us-central1",
            "deployment": {"runtime_kind": "cloud_run_service", "enabled": False},
            "logs": {"enabled": False, "allowed_logins": []},
        }
    ]
}
PRIVATE = deepcopy(PUBLIC)
PRIVATE["services"][0]["service_name"] = "private-example"
PRIVATE["services"][0]["logs"] = {"enabled": True, "allowed_logins": ["demo-reader"]}


def catalog_client(login="demo-reader", provider="github_oauth"):
    client = TestClient(app)
    if login:
        middleware = next(
            item
            for item in app.user_middleware
            if item.cls.__name__ == "SessionMiddleware"
        )
        session = {"github_login": login, "github_auth_provider": provider}
        client.cookies.set(
            "session",
            TimestampSigner(middleware.kwargs["secret_key"])
            .sign(b64encode(json.dumps(session).encode()))
            .decode(),
            domain="testserver.local",
            path="/",
        )
    return client


def encode(document):
    return json.dumps(document).encode()


def install_private(monkeypatch, tmp_path, raw=None):
    path = tmp_path / "mounted-catalog.json"
    raw = encode(PRIVATE) if raw is None else raw
    path.write_bytes(raw)
    monkeypatch.setattr(config, "catalog_path", str(path))
    monkeypatch.setattr(config, "catalog_sha256", hashlib.sha256(raw).hexdigest())
    return path


@pytest.fixture(autouse=True)
def isolate_source(monkeypatch, tmp_path):
    path = tmp_path / "public-fixture.json"
    path.write_bytes(encode(PUBLIC))
    monkeypatch.setattr(log_catalog, "CATALOG_PATH", path)
    monkeypatch.setattr(config, "catalog_path", None)
    monkeypatch.setattr(config, "catalog_sha256", None)
    monkeypatch.setattr(config, "mock_mode", True)
    monkeypatch.setattr(config.logs, "enabled", False)
    monkeypatch.setattr(config.logs, "allowed_logins", ("demo-reader",))
    monkeypatch.setattr(
        config.auth, "session_secret", "synthetic-session-secret-32-characters"
    )
    monkeypatch.setattr(config.auth, "github_client_id", "synthetic-client")
    monkeypatch.setattr(config.auth, "github_client_secret", "synthetic-secret")
    for key in ("ENG_PLATFORM_CATALOG_PATH", "ENG_PLATFORM_CATALOG_SHA256"):
        monkeypatch.delenv(key, raising=False)
    runtime_logs._caches.clear()
    yield
    runtime_logs._caches.clear()


def test_mounted_authority_is_the_only_source_without_cloud_clients(
    monkeypatch, tmp_path
):
    path = install_private(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.logs, "enabled", True)
    denied = Mock(
        side_effect=AssertionError("No cloud client belongs in catalog loading")
    )
    for module, name in (
        (catalog, "_run_client"),
        (catalog, "_job_client"),
        (runtime_logs, "_logging_client"),
        (log_budget, "firestore_client"),
    ):
        monkeypatch.setattr(module, name, denied)
    resources = log_catalog.resources()
    assert [r.service_id for r in resources] == ["private-example"]
    assert resources[0].can_read("demo-reader")
    response = catalog_client().get("/api/catalog/services")
    assert response.status_code == 200
    assert [r["service_name"] for r in response.json()["services"]] == [
        "private-example"
    ]
    assert "allowed_logins" not in response.text
    assert "demo-reader" not in response.text
    assert str(path) not in response.text
    assert config.catalog_sha256 not in response.text
    denied.assert_not_called()


@pytest.mark.parametrize(
    ("path", "pin"),
    [
        (None, "a" * 64),
        ("/missing.json", None),
        ("", "a" * 64),
        ("/missing.json", ""),
        ("", ""),
        ("relative.json", "a" * 64),
        ("/missing.json", "A" * 64),
        ("/missing.json", "z" * 64),
        ("/missing.json", "a" * 63),
        ("/bad\x00path", "a" * 64),
    ],
)
def test_partial_or_invalid_source_never_falls_back(monkeypatch, path, pin):
    monkeypatch.setattr(config, "catalog_path", path)
    monkeypatch.setattr(config, "catalog_sha256", pin)
    with pytest.raises(
        log_catalog.CatalogUnavailable, match="^Runtime catalog is unavailable$"
    ):
        log_catalog.load_catalog()
    if path is not None:
        monkeypatch.setenv("ENG_PLATFORM_CATALOG_PATH", path.replace("\x00", ""))
    if pin is not None:
        monkeypatch.setenv("ENG_PLATFORM_CATALOG_SHA256", pin)
    # NUL cannot be represented in the environment; runtime validation above
    # still rejects it if server configuration is changed in process.
    if path != "/bad\x00path":
        with pytest.raises(ValueError, match="private catalog"):
            load_config()


@pytest.mark.parametrize(
    "failure", ["missing", "tamper", "pin", "io", "directory", "fifo"]
)
def test_source_failures_are_sanitized_and_never_use_previous_or_public(
    monkeypatch, tmp_path, failure
):
    path = install_private(monkeypatch, tmp_path)
    assert log_catalog.resources()[0].can_read("demo-reader")
    if failure == "missing":
        path.unlink()
    elif failure == "tamper":
        path.write_bytes(encode(PUBLIC))
    elif failure == "pin":
        monkeypatch.setattr(config, "catalog_sha256", "0" * 64)
    elif failure == "io":
        monkeypatch.setattr(
            log_catalog.os, "open", Mock(side_effect=PermissionError("private error"))
        )
    else:
        path.unlink()
        path.mkdir() if failure == "directory" else os.mkfifo(path)
    with pytest.raises(
        log_catalog.CatalogUnavailable, match="^Runtime catalog is unavailable$"
    ):
        log_catalog.resources()
    if failure != "io":
        monkeypatch.setattr(config, "mock_mode", False)
        response = catalog_client().get("/api/catalog/services")
        assert response.status_code == 503
        assert response.json() == {"detail": "Runtime catalog is unavailable"}
        assert str(path) not in response.text


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"\xff",
        b'{"services":[],"services":[]}',
        b'{"services":NaN}',
        b'{"services":[]}',
        b'{"services":[{"service_name":"incomplete"}]}',
    ],
)
def test_correct_digest_does_not_bypass_strict_complete_validation(
    monkeypatch, tmp_path, raw
):
    install_private(monkeypatch, tmp_path, raw)
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.load_catalog()


def test_integrity_and_size_are_checked_before_parsing(monkeypatch, tmp_path):
    path = install_private(monkeypatch, tmp_path)
    denied = Mock(side_effect=AssertionError("Invalid source must not be parsed"))
    monkeypatch.setattr(log_catalog.json, "loads", denied)
    monkeypatch.setattr(config, "catalog_sha256", "0" * 64)
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.load_catalog()
    monkeypatch.setattr(
        config, "catalog_sha256", hashlib.sha256(path.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(log_catalog, "MAX_CATALOG_BYTES", 5)
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.load_catalog()
    denied.assert_not_called()


def test_production_requires_private_source_even_with_logs_disabled(
    monkeypatch, tmp_path
):
    assert log_catalog.resources()[0].service_id == "public-example"
    monkeypatch.setattr(config, "mock_mode", False)
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.resources()
    monkeypatch.setattr(config.logs, "enabled", True)
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.resources()
    install_private(monkeypatch, tmp_path)
    assert log_catalog.resources()[0].service_id == "private-example"
    monkeypatch.setattr(config, "catalog_sha256", None)
    monkeypatch.setattr(config.logs, "enabled", False)
    with pytest.raises(log_catalog.CatalogUnavailable):
        log_catalog.resources()


def test_environment_load_requires_pinned_source_only_for_live_production_logs(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("ENG_PLATFORM_MOCK_MODE", "false")
    monkeypatch.setenv("ENG_PLATFORM_LOGS_ENABLED", "false")
    assert load_config().catalog_path is None
    monkeypatch.setenv("ENG_PLATFORM_LOGS_ENABLED", "true")
    monkeypatch.setenv("ENG_PLATFORM_LOGS_QUOTA_PROJECT_ID", "quota-project")
    with pytest.raises(ValueError, match="private catalog"):
        load_config()
    monkeypatch.setenv("ENG_PLATFORM_MOCK_MODE", "true")
    assert load_config().logs.enabled
    path = install_private(monkeypatch, tmp_path)
    monkeypatch.setenv("ENG_PLATFORM_MOCK_MODE", "false")
    monkeypatch.setenv("ENG_PLATFORM_CATALOG_PATH", str(path))
    monkeypatch.setenv("ENG_PLATFORM_CATALOG_SHA256", config.catalog_sha256)
    loaded = load_config()
    assert loaded.catalog_path == str(path)
    assert loaded.catalog_sha256 == config.catalog_sha256
    assert loaded.logs.enabled and not loaded.mock_mode


@pytest.mark.parametrize("change", ["region", "reader", "disable"])
def test_new_pinned_policy_changes_resource_digest_and_invalidates_cache(
    monkeypatch, tmp_path, change
):
    path = install_private(monkeypatch, tmp_path)
    original = log_catalog.resources()[0]
    cache = runtime_logs._cache(original, [original])
    assert cache is not None and runtime_logs._is_current(cache)
    changed = deepcopy(PRIVATE)
    row = changed["services"][0]
    if change == "region":
        row["region"] = "europe-west1"
    elif change == "reader":
        row["logs"]["allowed_logins"] = ["another-reader"]
    else:
        row["logs"]["enabled"] = False
    raw = encode(changed)
    path.write_bytes(raw)
    # Updating bytes alone is never sufficient to alter the trusted authority.
    assert not runtime_logs._is_current(cache)
    assert cache.invalidated
    monkeypatch.setattr(config, "catalog_sha256", hashlib.sha256(raw).hexdigest())
    current = log_catalog.resources()[0]
    assert original.policy_fingerprint != current.policy_fingerprint
    assert runtime_logs._key(original) not in runtime_logs._caches
    assert not runtime_logs._is_current(cache)
    replacement = runtime_logs._cache(current, [current])
    assert replacement is not cache and runtime_logs._is_current(replacement)


def test_offline_explicit_path_is_not_a_runtime_source_switch(monkeypatch, tmp_path):
    install_private(monkeypatch, tmp_path)
    assert (
        log_catalog.load_catalog(log_catalog.CATALOG_PATH)[0]["service_name"]
        == "public-example"
    )
    assert log_catalog.resources()[0].service_id == "private-example"
