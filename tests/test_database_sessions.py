"""Fresh OAuth, copied-cookie replay and durable database-session revocation."""

from base64 import b64decode, b64encode
from copy import deepcopy
import hashlib
import json
from threading import RLock
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
import pytest

from eng_platform_api.config import config
from eng_platform_api.main import app
from eng_platform_api.routers import auth
from eng_platform_api.security import require_database_reader
from eng_platform_api.services import database_job_store as store
from eng_platform_api.services import database_sessions as sessions
from eng_platform_api import services


class SessionTestBackend:
    """Only an explicitly injected test dependency; no cloud credentials."""

    def __init__(self):
        self.records = {}
        self.lock = RLock()
        self.read_error = False
        self.write_error = False

    def get(self, kind, identity):
        if self.read_error:
            raise RuntimeError("provider-sensitive-diagnostic")
        with self.lock:
            return deepcopy(self.records.get(store.key(kind, identity)))

    def mutate(self, references, transform):
        if self.write_error:
            raise RuntimeError("provider-sensitive-diagnostic")
        with self.lock:
            values = {
                store.key(*reference): deepcopy(self.records.get(store.key(*reference)))
                for reference in references
            }
            replacements = transform(values)
            for key, value in replacements.items():
                if value is None:
                    self.records.pop(key, None)
                else:
                    self.records[key] = deepcopy(value)


@pytest.fixture
def configured(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "mock_mode", False)
    monkeypatch.setattr(config.databases, "enabled", True)
    monkeypatch.setattr(config.databases, "executions_enabled", True)
    monkeypatch.setattr(config.databases, "allowed_logins", ("reader", "other"))
    monkeypatch.setattr(config.databases, "project_id", "")
    monkeypatch.setattr(config.auth, "github_client_id", "session-test-client")
    monkeypatch.setattr(config.auth, "github_client_secret", "session-test-secret")
    monkeypatch.setattr(
        config.auth, "session_secret", "session-test-key-32-characters-long"
    )
    document = {
        "databases": [
            {
                "id": "sample",
                "name": "Sample",
                "schemas": ["sample"],
                "allowed_logins": ["reader", "other"],
                "dsn_env": "ENG_PLATFORM_DATABASE_DSN_SAMPLE",
            }
        ]
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(document))
    monkeypatch.setattr(config.databases, "registry_path", str(path))
    monkeypatch.setattr(
        config.databases,
        "registry_sha256",
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )
    backend = SessionTestBackend()
    with store.testing_backend(backend):
        yield backend


def request(nonce=None, *, login="reader", provider="github_oauth"):
    data = {"github_login": login, "github_auth_provider": provider}
    if nonce is not None:
        data[sessions.COOKIE_FIELD] = nonce
    return Request({"type": "http", "session": data, "headers": []})


def _signer():
    middleware = next(
        item for item in app.user_middleware if item.cls.__name__ == "SessionMiddleware"
    )
    return TimestampSigner(middleware.kwargs["secret_key"])


def _cookie(data):
    return _signer().sign(b64encode(json.dumps(data).encode())).decode()


def client(nonce=None, *, login="reader", provider="github_oauth"):
    result = TestClient(app, base_url="https://testserver")
    result.cookies.set(
        "session", _cookie(request(nonce, login=login, provider=provider).session)
    )
    return result


def _decoded_cookie(response):
    value = response.cookies.get("session")
    return json.loads(b64decode(_signer().unsign(value))) if value else {}


def _oauth(monkeypatch, *, login="reader"):
    authorize = Mock()
    authorize.create_authorization_url.side_effect = lambda _url, **kwargs: (
        f"https://github.com/login/oauth/authorize?state={kwargs['state']}",
        kwargs["state"],
    )
    token = Mock(fetch_token=AsyncMock(return_value={"access_token": "test-only"}))
    user = Mock()
    user.json.return_value = {
        "login": login,
        "avatar_url": "https://avatars.example/reader",
    }
    authenticated = Mock(get=AsyncMock(return_value=user))
    monkeypatch.setattr(
        auth, "AsyncOAuth2Client", Mock(side_effect=[authorize, token, authenticated])
    )
    return token, authenticated


def _callback(browser):
    login = browser.get("/api/auth/login", follow_redirects=False)
    state = _decoded_cookie(login)["oauth_state"]
    return browser.get(
        f"/api/auth/callback?state={state}&code=test-only", follow_redirects=False
    )


def test_nonce_is_opaque_hash_is_stored_and_login_is_bound(configured):
    nonce = sessions.create("Reader")
    record = sessions.require(request(nonce))
    assert record["id"] == hashlib.sha256(nonce.encode()).hexdigest()
    assert record["login"] == "reader"
    assert (
        record["expires_at"] - record["created_at"] == sessions.SESSION_LIFETIME_SECONDS
    )
    assert nonce not in json.dumps(configured.records)
    assert sessions.validate(record["id"], "Reader") == record
    with pytest.raises(HTTPException) as denied:
        sessions.validate(record["id"], "other")
    assert denied.value.status_code == 401


@pytest.mark.parametrize("provider", ["mock", "iap", "", "github"])
def test_only_real_github_oauth_cookie_qualifies(configured, provider):
    nonce = sessions.create("reader")
    with pytest.raises(HTTPException) as denied:
        sessions.require(request(nonce, provider=provider))
    assert denied.value.status_code == 401


@pytest.mark.parametrize(
    "nonce", [None, "", "copied-old-cookie", 123, "x" * 42, "x" * 44]
)
def test_old_or_malformed_signed_cookie_needs_new_handshake(configured, nonce):
    with pytest.raises(HTTPException) as denied:
        require_database_reader(request(nonce))
    assert denied.value.status_code == 401
    response = client(nonce).get("/api/auth/me")
    assert response.json()["can_query_databases"] is False
    assert response.json()["database_session_required"] is True


def test_fixed_expiry_does_not_slide_with_cookie_refresh(configured, monkeypatch):
    monkeypatch.setattr(sessions, "time", lambda: 1000.0)
    nonce = sessions.create("reader")
    monkeypatch.setattr(
        sessions, "time", lambda: 1000.0 + sessions.SESSION_LIFETIME_SECONDS - 1
    )
    assert sessions.require(request(nonce))["expires_at"] == 44200.0
    monkeypatch.setattr(sessions, "time", lambda: 44200.0)
    with pytest.raises(HTTPException) as denied:
        sessions.require(request(nonce))
    assert denied.value.status_code == 401


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "execution"},
        {"id": "other"},
        {"login": "other"},
        {"expires_at": float("inf")},
        {"created_at": float("nan")},
        {"revoked_at": None},
        {"revoked_at": True},
        {"revoked_at": 1},
        {"expires_at": "99999999999"},
        {"expires_at": 10**10000},
    ],
)
def test_corrupt_or_revoked_session_cannot_authorize(configured, change):
    nonce = sessions.create("reader")
    session_id = sessions.require(request(nonce))["id"]
    configured.records[f"session:{session_id}"].update(change)
    with pytest.raises(HTTPException) as denied:
        sessions.require(request(nonce))
    assert denied.value.status_code == 401


def test_acl_removal_is_rechecked_by_workers_without_cookie(configured, monkeypatch):
    nonce = sessions.create("reader")
    session_id = sessions.require(request(nonce))["id"]
    monkeypatch.setattr(config.databases, "allowed_logins", ("other",))
    with pytest.raises(HTTPException) as denied:
        sessions.validate(session_id, "reader")
    assert denied.value.status_code == 403
    assert (
        client(nonce).get("/api/auth/me").json()["database_session_required"] is False
    )


def test_revocation_is_durable_idempotent_and_replay_denied(configured):
    nonce = sessions.create("reader")
    original = request(nonce)
    session_id = sessions.revoke(original)
    first = store.get("session", session_id)
    assert first["revoked_at"] > 0
    assert sessions.revoke(original) == session_id
    assert store.get("session", session_id)["revoked_at"] == first["revoked_at"]
    with pytest.raises(HTTPException) as denied:
        sessions.require(request(nonce))
    assert denied.value.status_code == 401


def test_session_outage_is_generic_and_never_offers_renewal(configured):
    nonce = sessions.create("reader")
    configured.read_error = True
    with pytest.raises(HTTPException) as denied:
        sessions.require(request(nonce))
    assert denied.value.status_code == 503
    assert "provider-sensitive" not in denied.value.detail
    response = client(nonce).get("/api/auth/me")
    assert response.json()["can_query_databases"] is False
    assert response.json()["database_session_required"] is False


def test_no_unconfigured_production_fallback(configured):
    with store.testing_backend(None):
        with pytest.raises(HTTPException) as denied:
            sessions.create("reader")
    assert denied.value.status_code == 503
    assert configured.records == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("github_client_id", ""),
        ("github_client_secret", ""),
        ("session_secret", "too-short"),
    ],
)
def test_create_needs_real_strong_oauth_configuration(
    configured, monkeypatch, field, value
):
    monkeypatch.setattr(config.auth, field, value)
    with pytest.raises(HTTPException) as denied:
        sessions.create("reader")
    assert denied.value.status_code == 503
    assert configured.records == {}


def test_workers_do_not_need_browser_signing_or_github_secrets(configured, monkeypatch):
    nonce = sessions.create("reader")
    session_id = sessions.require(request(nonce))["id"]
    monkeypatch.setattr(config.auth, "session_secret", "")
    monkeypatch.setattr(config.auth, "github_client_secret", "")
    assert sessions.validate(session_id, "reader")["login"] == "reader"


def test_create_rejects_mock_even_with_explicit_test_backend(configured, monkeypatch):
    monkeypatch.setattr(config, "mock_mode", True)
    with pytest.raises(HTTPException) as denied:
        sessions.create("reader")
    assert denied.value.status_code == 401
    assert configured.records == {}


def test_disabled_execution_mode_preserves_legacy_reader(configured, monkeypatch):
    monkeypatch.setattr(config.databases, "executions_enabled", False)
    assert require_database_reader(request()) == "reader"
    assert client().get("/api/auth/me").json()["database_session_required"] is False


def test_real_callback_mints_fresh_database_nonce(configured, monkeypatch):
    _oauth(monkeypatch)
    response = _callback(TestClient(app, base_url="https://testserver"))
    assert response.status_code == 307
    cookie = _decoded_cookie(response)
    assert cookie["github_auth_provider"] == "github_oauth"
    assert require_database_reader(request(cookie[sessions.COOKIE_FIELD])) == "reader"


def test_repeated_oauth_revokes_old_nonce_before_purge_and_rotates(
    configured, monkeypatch
):
    old_nonce = sessions.create("reader")
    old_id = sessions.require(request(old_nonce))["id"]
    purged = []

    def purge(session_id):
        assert store.get("session", session_id)["revoked_at"] > 0
        purged.append(session_id)

    monkeypatch.setattr(
        services, "database_jobs", SimpleNamespace(purge_session=purge), raising=False
    )
    _oauth(monkeypatch)
    response = _callback(client(old_nonce))
    assert response.status_code == 307
    new_nonce = _decoded_cookie(response)[sessions.COOKIE_FIELD]
    assert new_nonce != old_nonce
    assert purged == [old_id]
    assert sessions.require(request(new_nonce))["id"] != old_id
    with pytest.raises(HTTPException) as denied:
        sessions.require(request(old_nonce))
    assert denied.value.status_code == 401


def test_disabled_real_callback_does_not_mint_db_session(configured, monkeypatch):
    monkeypatch.setattr(config.databases, "executions_enabled", False)
    _oauth(monkeypatch)
    response = _callback(TestClient(app, base_url="https://testserver"))
    assert response.status_code == 307
    assert sessions.COOKIE_FIELD not in _decoded_cookie(response)
    assert configured.records == {}


def test_callback_storage_failure_never_grants_database_session(
    configured, monkeypatch
):
    _oauth(monkeypatch)
    configured.write_error = True
    response = _callback(TestClient(app, base_url="https://testserver"))
    assert response.status_code == 503
    cookie = _decoded_cookie(response)
    assert sessions.COOKIE_FIELD not in cookie
    assert "provider-sensitive" not in response.text


def test_mock_callback_does_not_mint_nonce_when_oauth_is_mocked(
    configured, monkeypatch
):
    token, authenticated = _oauth(monkeypatch)
    monkeypatch.setattr(config, "mock_mode", True)
    browser = client()
    state = "fake-handshake"
    browser.cookies.set("session", _cookie({"oauth_state": state}))
    # A direct mocked callback must still fail to mint a production nonce.
    auth.AsyncOAuth2Client.side_effect = [token, authenticated]
    response = browser.get(
        f"/api/auth/callback?state={state}&code=test-only", follow_redirects=False
    )
    assert response.status_code == 307
    assert sessions.COOKIE_FIELD not in _decoded_cookie(response)
    assert configured.records == {}


def test_logout_revokes_before_clear_and_old_cookie_cannot_replay(configured):
    nonce = sessions.create("reader")
    browser = client(nonce)
    logout = browser.post("/api/auth/logout")
    assert logout.status_code == 200
    assert logout.json()["authenticated"] is False
    replay = client(nonce).get("/api/auth/me")
    assert replay.json()["authenticated"] is True
    assert replay.json()["can_query_databases"] is False
    assert replay.json()["database_session_required"] is True


def test_logout_cleanup_failure_does_not_reactivate_revoked_session(
    configured, monkeypatch
):
    nonce = sessions.create("reader")
    session_id = sessions.require(request(nonce))["id"]
    purge = Mock(side_effect=RuntimeError("cleanup-sensitive-diagnostic"))
    monkeypatch.setattr(
        services, "database_jobs", SimpleNamespace(purge_session=purge), raising=False
    )
    response = client(nonce).post("/api/auth/logout")
    assert response.status_code == 200
    purge.assert_called_once_with(session_id)
    assert store.get("session", session_id)["revoked_at"] > 0
    with pytest.raises(HTTPException) as denied:
        sessions.validate(session_id, "reader")
    assert denied.value.status_code == 401


def test_uncertain_revocation_commit_remains_revoked_but_logout_fails(
    configured, monkeypatch
):
    nonce = sessions.create("reader")
    session_id = sessions.require(request(nonce))["id"]
    original = configured.mutate

    def uncertain(references, transform):
        original(references, transform)
        raise RuntimeError("acknowledgement-lost-sensitive-diagnostic")

    monkeypatch.setattr(configured, "mutate", uncertain)
    browser = client(nonce)
    response = browser.post("/api/auth/logout")
    assert response.status_code == 503
    assert _decoded_cookie(browser)[sessions.COOKIE_FIELD] == nonce
    assert store.get("session", session_id)["revoked_at"] > 0
    assert "sensitive-diagnostic" not in response.text


def test_failed_revocation_blocks_logout_and_keeps_nonce(configured):
    nonce = sessions.create("reader")
    configured.write_error = True
    browser = client(nonce)
    response = browser.post("/api/auth/logout")
    assert response.status_code == 503
    assert _decoded_cookie(browser)[sessions.COOKIE_FIELD] == nonce
    assert "provider-sensitive" not in response.text


def test_logout_missing_nonce_does_not_contact_store(configured):
    configured.write_error = True
    response = client().post("/api/auth/logout")
    assert response.status_code == 200


def test_disabled_executions_still_revoke_issued_nonce(configured, monkeypatch):
    nonce = sessions.create("reader")
    session_id = sessions.require(request(nonce))["id"]
    monkeypatch.setattr(config.databases, "executions_enabled", False)
    assert client(nonce).post("/api/auth/logout").status_code == 200
    assert store.get("session", session_id)["revoked_at"] > 0
