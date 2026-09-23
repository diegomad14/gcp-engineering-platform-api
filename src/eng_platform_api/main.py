"""Engineering Platform Control Plane API.

FastAPI application with mock-backed endpoints.
All GCP integrations require explicit configuration.
"""

import logging
import secrets
from contextlib import asynccontextmanager
from time import monotonic

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import BaseRoute, Match, NoMatchFound

from .config import config
from .routers import (
    auth,
    catalog,
    costs,
    deployments,
    deployment_events,
    github_events,
    health,
    metrics,
    quality,
    release_authorizations,
    release_execution_events,
    release_operations,
    releases,
    service_factory,
    secrets as operational_secrets_router,
)
from .mcp_server import mcp as mcp_server


class _FeatureFlagMCPApp:
    """Keep the remote surface completely undiscoverable until activation."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if not config.mcp.enabled:
            await send({"type": "http.response.start", "status": 404, "headers": []})
            await send({"type": "http.response.body", "body": b"Not Found"})
            return
        await self.app(scope, receive, send)


class _MCPRoute(BaseRoute):
    """Route only MCP/OAuth metadata paths; never swallow normal API redirects."""

    _paths = {
        "/mcp",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource/mcp",
        "/authorize",
        "/token",
        "/register",
        "/revoke",
    }

    def __init__(self, app):
        self.app = app

    def matches(self, scope):
        return (
            (Match.FULL, {}) if scope.get("path") in self._paths else (Match.NONE, {})
        )

    async def handle(self, scope, receive, send):
        await self.app(scope, receive, send)

    def url_path_for(self, name: str, /, **path_params):
        raise NoMatchFound(name, path_params)


_mcp_asgi: _FeatureFlagMCPApp | None = None


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """Run the MCP session manager even though it is mounted below FastAPI."""
    if not config.mcp.enabled:
        yield
        return
    # FastMCP session managers are intentionally single-use. Recreate the
    # mounted sub-app for a process restart (and isolated TestClient lifespans).
    mcp_server._session_manager = None
    assert _mcp_asgi is not None
    _mcp_asgi.app = mcp_server.streamable_http_app()
    async with mcp_server.session_manager.run():
        yield


app = FastAPI(
    title="Engineering Platform API",
    version="0.5.0",
    description="Control plane API for the GCP Engineering Platform. "
    "All endpoints use mock data unless GCP credentials are configured.",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=_lifespan,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=config.auth.session_secret or secrets.token_urlsafe(32),
    same_site="lax" if config.mock_mode else "none",
    https_only=not config.mock_mode,
    max_age=60 * 60 * 12,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[config.auth.frontend_url],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(health.router)
app.include_router(catalog.router)
app.include_router(releases.router)
app.include_router(deployments.router)
app.include_router(deployment_events.router)
app.include_router(github_events.router)
app.include_router(release_execution_events.router)
app.include_router(release_operations.router)
app.include_router(metrics.router)
app.include_router(costs.router)
app.include_router(quality.router)
app.include_router(release_authorizations.router)
app.include_router(service_factory.router)
app.include_router(operational_secrets_router.router)

logger = logging.getLogger("eng_platform_api.requests")


@app.middleware("http")
async def record_request_duration(request: Request, call_next):
    started = monotonic()
    response = await call_next(request)
    if request.url.path.startswith("/api/services/") and "/secrets" in request.url.path:
        # Include validation/provider failures, not only successful responses.
        response.headers["Cache-Control"] = "no-store"
    duration_ms = round((monotonic() - started) * 1000, 2)
    response.headers["X-Process-Time-Ms"] = str(duration_ms)
    log = logger.warning if duration_ms >= 1000 else logger.info
    log(
        "request_complete method=%s path=%s status=%s duration_ms=%.2f",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "Engineering Platform API",
        "version": "0.5.0",
        "docs": "/docs",
    }


@app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
async def oauth_authorization_server_metadata():
    """Publish metadata matching the public-PKCE DCR provider contract."""
    if not config.mcp.enabled:
        return Response(status_code=404)
    issuer = (config.mcp.issuer_url or config.mcp.public_base_url).rstrip("/")
    return JSONResponse(
        {
            "issuer": issuer + "/",
            "authorization_endpoint": issuer + "/authorize",
            "token_endpoint": issuer + "/token",
            "registration_endpoint": issuer + "/register",
            "scopes_supported": [
                "eng-platform.deploy",
                "eng-platform.read",
                "eng-platform.rollback",
            ],
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint": issuer + "/revoke",
            "revocation_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
        }
    )


# The MCP SDK provides RFC 9728 metadata, DCR, authorization, token,
# revocation, and the exact Streamable HTTP route under this same API origin.
# Mount last so the regular FastAPI routes always win.
_mcp_asgi = _FeatureFlagMCPApp(mcp_server.streamable_http_app())
app.router.routes.append(_MCPRoute(_mcp_asgi))
