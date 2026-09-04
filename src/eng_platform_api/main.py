"""Engineering Platform Control Plane API.

FastAPI application with mock-backed endpoints.
All GCP integrations require explicit configuration.
"""

import logging
import secrets
from time import monotonic

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware

from .config import config
from .routers import (
    auth,
    catalog,
    costs,
    deployments,
    health,
    metrics,
    quality,
    release_authorizations,
    releases,
    service_factory,
    secrets as operational_secrets_router,
)

app = FastAPI(
    title="Engineering Platform API",
    version="0.5.0",
    description="Control plane API for the GCP Engineering Platform. "
    "All endpoints use mock data unless GCP credentials are configured.",
    docs_url="/docs",
    redoc_url="/redoc",
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
