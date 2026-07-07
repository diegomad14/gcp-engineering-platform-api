"""Security utilities for the Engineering Platform API.

MVP: No authentication. Document that production requires IAP/OAuth before
exposing the platform API publicly.

Do NOT hardcode tokens, keys, or credentials here.
"""

from fastapi import Request


def get_identity(request: Request) -> str:
    """Return the caller identity.

    MVP: Returns 'anonymous' since no auth is configured.
    Production: Extract from IAP/OAuth headers.
    """
    # Future: extract from X-Goog-Authenticated-User-Email (IAP)
    # Future: extract from Authorization header (OAuth)
    return "anonymous"


def verify_no_secrets_in_response(data: dict) -> dict:
    """Sanitize response data to ensure no secrets leak.

    This is a safety net. All service modules should avoid
    returning secrets in the first place.
    """
    forbidden_keys = {
        "token", "password", "secret", "key", "credential",
        "sonar_token", "api_key", "private_key",
    }
    if isinstance(data, dict):
        return {
            k: v for k, v in data.items()
            if k.lower() not in forbidden_keys
        }
    return data
