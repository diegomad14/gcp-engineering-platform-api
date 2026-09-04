"""Tests for platform-issued, one-time GitHub workflow authorizations."""

from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from eng_platform_api.config import config
from eng_platform_api.main import app
from eng_platform_api.services import release_authorization
from eng_platform_api.services import release_authorization_store


def _private_key_pem() -> str:
    return (
        Ed25519PrivateKey.generate()
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )


def _expected():
    return {
        "repository": "diegomad14/example-service",
        "service_name": "example-api",
        "tag": "v1.2.3",
        "sha": "a" * 40,
        "github_deployment_id": "123",
        "kind": "deploy",
    }


def test_issue_and_verify_round_trip():
    with mock.patch.object(
        config.github, "release_signing_private_key", _private_key_pem()
    ):
        token, claims = release_authorization.issue(
            repository=_expected()["repository"],
            service_name=_expected()["service_name"],
            tag=_expected()["tag"],
            sha=_expected()["sha"],
            github_deployment_id=123,
            requested_by="angelmarin1122-coder",
            kind="deploy",
        )
        verified = release_authorization.verify(token, _expected())
    assert verified["jti"] == claims["jti"]
    assert verified["requested_by"] == "angelmarin1122-coder"


def test_verify_rejects_changed_release_context():
    with mock.patch.object(
        config.github, "release_signing_private_key", _private_key_pem()
    ):
        token, _ = release_authorization.issue(
            repository=_expected()["repository"],
            service_name=_expected()["service_name"],
            tag=_expected()["tag"],
            sha=_expected()["sha"],
            github_deployment_id=123,
            requested_by="diegomad14",
            kind="deploy",
        )
        bad = {**_expected(), "tag": "v9.9.9"}
        try:
            release_authorization.verify(token, bad)
        except release_authorization.ReleaseAuthorizationError as exc:
            assert "tag" in str(exc)
        else:
            raise AssertionError("changed release context was accepted")


def test_consume_is_one_time_only(tmp_path):
    expected = _expected()
    with (
        mock.patch.object(
            config.github, "release_signing_private_key", _private_key_pem()
        ),
        mock.patch.object(
            release_authorization_store, "_DEFAULT_STORE_PATH", tmp_path / "auth.json"
        ),
        mock.patch.dict(
            "os.environ", {"ENG_PLATFORM_RELEASE_AUTH_FIRESTORE_COLLECTION": ""}
        ),
    ):
        token, _ = release_authorization.issue(
            repository=expected["repository"],
            service_name=expected["service_name"],
            tag=expected["tag"],
            sha=expected["sha"],
            github_deployment_id=123,
            requested_by="angelmarin1122-coder",
            kind="deploy",
        )
        client = TestClient(app)
        body = {"token": token, **expected}
        first = client.post("/api/internal/release-authorizations/consume", json=body)
        second = client.post("/api/internal/release-authorizations/consume", json=body)
    assert first.status_code == 200
    assert first.json()["accepted"] is True
    assert second.status_code == 409
