"""Security utilities for the Engineering Platform API.

Production deploy actions require a verified GitHub OAuth session. IAP identity
headers are accepted only when the deployment is explicitly behind a trusted
IAP boundary.

Do NOT hardcode tokens, keys, or credentials here.
"""

import hmac
import os

from fastapi import Header, HTTPException, Request, status

from .config import config


def get_identity(request: Request) -> str:
    """Return the caller identity.

    MVP: Returns 'anonymous' since no auth is configured.
    Production: Extract from IAP/OAuth headers.
    """
    iap_identity = request.headers.get("X-Goog-Authenticated-User-Email", "")
    if config.auth.trust_iap_identity and iap_identity:
        return iap_identity.removeprefix("accounts.google.com:")
    github_identity = str(request.session.get("github_login", ""))
    return github_identity or ("diegomad14" if config.mock_mode else "anonymous")


def require_deployer(request: Request) -> str:
    """Require an authenticated, allowlisted platform deployer."""
    identity = get_identity(request)
    if identity == "anonymous":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in with GitHub to deploy a service",
        )
    allowed = config.auth.allowed_logins
    if allowed and identity.lower() not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"GitHub user '{identity}' is not allowed to deploy",
        )
    return identity


def verify_no_secrets_in_response(data: dict) -> dict:
    """Sanitize response data to ensure no secrets leak.

    This is a safety net. All service modules should avoid
    returning secrets in the first place.
    """
    forbidden_keys = {
        "token",
        "password",
        "secret",
        "key",
        "credential",
        "sonar_token",
        "api_key",
        "private_key",
    }
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if k.lower() not in forbidden_keys}
    return data


def require_quality_ingest_token(
    request: Request,
    authorization: str | None = Header(default=None),
) -> None:
    """Require the organization quality-ingest token for report writes."""
    content_length = request.headers.get("content-length")
    oversized = False
    try:
        if content_length is not None:
            oversized = int(content_length) > 1_000_000
    except ValueError:
        oversized = True
    if oversized:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Quality report exceeds the 1 MB limit",
        )
    expected = os.getenv("ENG_PLATFORM_QUALITY_INGEST_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Quality report ingestion is not configured",
        )
    scheme, _, supplied = (authorization or "").partition(" ")
    if (
        scheme.lower() != "bearer"
        or not supplied
        or not hmac.compare_digest(supplied, expected)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid quality report token",
            headers={"WWW-Authenticate": "Bearer"},
        )
