"""Cryptographic OIDC verification and exact trusted workflow boundaries."""

import time
from types import SimpleNamespace
from unittest import mock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from eng_platform_api.services import workflow_identity as subject
from eng_platform_api.services.release_authorization import ReleaseAuthorizationError
from eng_platform_api.services import release_authorization_store

REPOSITORY = "owner/engine"


@pytest.fixture
def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def claims():
    now = int(time.time())
    return {
        "iss": subject.ISSUER,
        "aud": subject.AUDIENCE,
        "iat": now,
        "nbf": now - 1,
        "exp": now + 300,
        "sub": "repo:owner/engine:ref:refs/heads/main",
        "repository": REPOSITORY,
        "ref": "refs/heads/main",
        "workflow_ref": "owner/engine/.github/workflows/central-release.yml@refs/heads/main",
        "run_id": "123",
        "run_attempt": "1",
        "event_name": "workflow_dispatch",
    }


def verify(key, data):
    token = jwt.encode(data, key, algorithm="RS256", headers={"kid": "test"})
    with mock.patch.object(
        subject._keys,
        "get_signing_key_from_jwt",
        return_value=SimpleNamespace(key=key.public_key()),
    ):
        return subject.verify(token, REPOSITORY, "deploy")


def test_valid_oidc_only_returns_sanitized_identity(key):
    result = verify(key, claims())
    assert result["run_id"] == "123"
    assert set(result) == {"run_id", "run_attempt", "workflow_ref", "repository"}


@pytest.mark.parametrize(
    "field,value",
    [
        ("iss", "https://attacker.example"),
        ("aud", "other"),
        ("repository", "owner/service"),
        ("ref", "refs/heads/feature"),
        ("workflow_ref", "owner/engine/.github/workflows/other.yml@refs/heads/main"),
        (
            "job_workflow_ref",
            "owner/untrusted/.github/workflows/reusable.yml@refs/heads/main",
        ),
        ("event_name", "pull_request"),
        ("exp", 1),
    ],
)
def test_untrusted_or_expired_identity_is_rejected(key, field, value):
    with pytest.raises(ReleaseAuthorizationError):
        verify(key, claims() | {field: value})


def test_missing_required_claim_is_rejected(key):
    data = claims()
    del data["run_id"]
    with pytest.raises(ReleaseAuthorizationError):
        verify(key, data)


def test_invalid_signature_is_rejected(key):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(claims(), other, algorithm="RS256")
    with mock.patch.object(
        subject._keys,
        "get_signing_key_from_jwt",
        return_value=SimpleNamespace(key=key.public_key()),
    ):
        with pytest.raises(ReleaseAuthorizationError):
            subject.verify(token, REPOSITORY, "deploy")


def test_missing_identity_is_rejected():
    with pytest.raises(ReleaseAuthorizationError):
        subject.verify("", REPOSITORY, "deploy")


def test_central_authorization_never_falls_back_to_local_storage(monkeypatch):
    monkeypatch.delenv("ENG_PLATFORM_RELEASE_AUTH_FIRESTORE_COLLECTION", raising=False)
    with pytest.raises(RuntimeError, match="Durable"):
        release_authorization_store.consume("ticket", {}, require_durable=True)
