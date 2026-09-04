"""Short-lived, signed authorizations for GitHub release workflows."""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from ..config import config

ISSUER = "engineering-platform"
AUDIENCE = "github-release-workflow"
TOKEN_TTL_SECONDS = 300


class ReleaseAuthorizationError(ValueError):
    """Raised when a release authorization is invalid or does not match."""


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _json_segment(value: dict[str, Any]) -> str:
    return _b64encode(json.dumps(value, separators=(",", ":"), sort_keys=True).encode())


def _private_key() -> Ed25519PrivateKey:
    raw = config.github.release_signing_private_key.strip()
    if not raw:
        raise RuntimeError("Release authorization signing key is not configured")
    key = serialization.load_pem_private_key(raw.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise RuntimeError("Release authorization signing key must be Ed25519")
    return key


def _public_key() -> Ed25519PublicKey:
    raw = config.github.release_signing_public_key.strip()
    if raw:
        key = serialization.load_pem_public_key(raw.encode())
    else:
        key = _private_key().public_key()
    if not isinstance(key, Ed25519PublicKey):
        raise RuntimeError("Release authorization verification key must be Ed25519")
    return key


def issue(
    *,
    repository: str,
    service_name: str,
    tag: str,
    sha: str,
    github_deployment_id: int,
    requested_by: str,
    kind: str,
    target_revision: str = "",
    execution_repository: str = "",
    configuration: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
        "jti": str(uuid.uuid4()),
        "repository": repository,
        "service_name": service_name,
        "tag": tag,
        "sha": sha,
        "github_deployment_id": str(github_deployment_id),
        "requested_by": requested_by,
        "kind": kind,
    }
    if target_revision:
        claims["target_revision"] = target_revision
    if execution_repository:
        claims["execution_repository"] = execution_repository
        claims["configuration_hash"] = hashlib.sha256(
            json.dumps(
                configuration or {}, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    header = {"alg": "EdDSA", "typ": "JWT", "kid": "release-v1"}
    signing_input = f"{_json_segment(header)}.{_json_segment(claims)}".encode()
    token = f"{signing_input.decode()}.{_b64encode(_private_key().sign(signing_input))}"
    return token, claims


def verify(token: str, expected: dict[str, str]) -> dict[str, Any]:
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
        header = json.loads(_b64decode(header_segment))
        claims = json.loads(_b64decode(payload_segment))
        signature = _b64decode(signature_segment)
    except ValueError as exc:
        raise ReleaseAuthorizationError("Malformed release authorization") from exc
    if header != {"alg": "EdDSA", "kid": "release-v1", "typ": "JWT"}:
        raise ReleaseAuthorizationError("Unsupported release authorization header")
    try:
        _public_key().verify(signature, f"{header_segment}.{payload_segment}".encode())
    except Exception as exc:
        raise ReleaseAuthorizationError(
            "Invalid release authorization signature"
        ) from exc
    now = int(time.time())
    if (
        claims.get("iss") != ISSUER
        or claims.get("aud") != AUDIENCE
        or not isinstance(claims.get("iat"), int)
        or not isinstance(claims.get("exp"), int)
        or claims["iat"] > now + 30
        or claims["exp"] < now
        or claims["exp"] - claims["iat"] > TOKEN_TTL_SECONDS
        or not claims.get("jti")
    ):
        raise ReleaseAuthorizationError("Expired or invalid release authorization")
    for key, value in expected.items():
        if str(claims.get(key, "")) != value:
            raise ReleaseAuthorizationError(f"Release authorization mismatch: {key}")
    return claims
