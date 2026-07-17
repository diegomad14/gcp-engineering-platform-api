"""Engineering Platform Control Plane API.

FastAPI application with mock-backed endpoints.
All GCP integrations require explicit configuration.
"""

import secrets

from fastapi import FastAPI
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
    releases,
    service_factory,
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
    CORSMiddleware,
    allow_origins=[config.auth.frontend_url],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.auth.session_secret or secrets.token_urlsafe(32),
    same_site="lax" if config.mock_mode else "none",
    https_only=not config.mock_mode,
    max_age=60 * 60 * 12,
)

app.include_router(auth.router)
app.include_router(health.router)
app.include_router(catalog.router)
app.include_router(releases.router)
app.include_router(deployments.router)
app.include_router(metrics.router)
app.include_router(costs.router)
app.include_router(quality.router)
app.include_router(service_factory.router)


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "Engineering Platform API",
        "version": "0.5.0",
        "docs": "/docs",
    }
