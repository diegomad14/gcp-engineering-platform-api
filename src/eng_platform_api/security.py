"""Security utilities for the Engineering Platform API.

Production deploy actions require a verified GitHub OAuth session. IAP identity
headers are accepted only when the deployment is explicitly behind a trusted
IAP boundary.

Do NOT hardcode tokens, keys, or credentials here.
"""

import hmac
import os

from fastapi import Header, HTTPException, Request, status

from .config import catalog_source_identity, config


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
    if not allowed or identity.lower() not in allowed:
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


def log_reader_identity(request: Request) -> str:
    """Only a real, signed GitHub OAuth session may read production logs.

    In particular, the development login and trusted IAP header never qualify.
    Sessions predating the provenance marker must sign in again.
    """
    if (
        config.mock_mode
        or request.session.get("github_auth_provider") != "github_oauth"
    ):
        return ""
    identity = request.session.get("github_login")
    return identity if isinstance(identity, str) else ""


def log_auth_configured() -> bool:
    return bool(
        len(config.auth.session_secret) >= 32
        and config.auth.github_client_id
        and config.auth.github_client_secret
    )


def can_view_logs(request: Request) -> bool:
    identity = log_reader_identity(request)
    return bool(
        config.logs.enabled
        and log_auth_configured()
        and identity
        and identity.lower() in config.logs.allowed_logins
    )


def require_log_reader(request: Request) -> str:
    """Logs authorization is separate from deployment permission."""
    identity = log_reader_identity(request)
    if not identity:
        raise HTTPException(status_code=401, detail="Sign in with GitHub to view logs")
    if not log_auth_configured() or identity.lower() not in config.logs.allowed_logins:
        raise HTTPException(status_code=403, detail="You are not allowed to view logs")
    return identity


def require_database_reader(request: Request) -> str:
    """Database ACL is independent from deployers and runtime-log readers."""
    identity = log_reader_identity(request)
    if not identity:
        raise HTTPException(401, "Sign in with GitHub to query databases")
    if (
        not config.databases.enabled
        or not log_auth_configured()
        or identity.lower() not in config.databases.allowed_logins
    ):
        raise HTTPException(403, "You are not allowed to query databases")
    return identity


def can_query_databases(request: Request) -> bool:
    from .services.database_registry import DatabaseUnavailable, databases

    try:
        identity = require_database_reader(request)
        return any(item.can_read(identity) for item in databases())
    except (HTTPException, DatabaseUnavailable):
        return False


def private_catalog_required() -> bool:
    """Only explicit mock mode without a configured source is public."""
    return bool(
        not config.mock_mode
        or config.catalog_path is not None
        or config.catalog_sha256 is not None
    )


def require_private_catalog_reader(
    identity: str, service_name: str | None = None, *, filtered_catalog: bool = False
) -> set[str] | None:
    """Revalidate source and ACL before any private metadata provider/cache hit.

    A catalog list can be filtered before computing readiness. Other existing
    aggregate DTOs are not partitioned by reader, so deny those aggregates if
    any resource is hidden rather than returning a broader cached result.
    """
    if not private_catalog_required():
        return None
    if not identity:
        raise HTTPException(401, "Sign in with GitHub to view the catalog")
    if identity.lower() not in config.logs.allowed_logins:
        raise HTTPException(403, "You are not allowed to view this metadata")
    from .services import log_catalog

    try:
        resources = log_catalog.resources()
    except log_catalog.CatalogUnavailable:
        raise HTTPException(503, "Runtime catalog is unavailable") from None
    visible = {item.service_id for item in resources if item.can_read(identity)}
    if service_name:
        if service_name not in {item.service_id for item in resources}:
            raise HTTPException(404, "Service not found")
        if service_name not in visible:
            raise HTTPException(403, "You are not allowed to view this metadata")
    elif not visible or (not filtered_catalog and len(visible) != len(resources)):
        raise HTTPException(403, "You are not allowed to view this metadata")
    return visible


def can_view_catalog(request: Request) -> bool:
    if not private_catalog_required():
        return True
    if not log_auth_configured():
        return False
    try:
        return bool(
            require_private_catalog_reader(
                log_reader_identity(request), filtered_catalog=True
            )
        )
    except HTTPException:
        return False


def is_private_metadata_request(request: Request) -> bool:
    """Keep machine evidence, ingestion, internal callbacks and auth unchanged."""
    if not private_catalog_required():
        return False
    path = request.url.path
    if request.method == "POST":
        # Planning checks the authority for existing identities, so it must not
        # become an anonymous resource-existence oracle.
        return path == "/api/service-factory/plan"
    if request.method != "GET":
        return False
    return bool(
        path.startswith(
            (
                "/api/catalog/",
                "/api/deployments/",
                "/api/services/",
                "/api/metrics/",
                "/api/costs/",
                "/api/releases/",
            )
        )
        or path in {"/api/health/services", "/api/quality/summary", "/api/releases"}
        or (path.startswith("/api/quality/services/") and path.endswith("/reports"))
    )


def require_private_metadata_request(request: Request) -> None:
    if not is_private_metadata_request(request):
        return
    identity = log_reader_identity(request)
    if identity and not log_auth_configured():
        raise HTTPException(403, "You are not allowed to view this metadata")
    request.state.catalog_visible_services = require_private_catalog_reader(
        identity,
        request.path_params.get("service_name"),
        filtered_catalog=request.url.path == "/api/catalog/services",
    )
    request.state.private_catalog_source = catalog_source_identity()
