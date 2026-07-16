"""Engineering Platform Control Plane API.

FastAPI application with mock-backed endpoints.
All GCP integrations require explicit configuration.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routers import catalog, costs, health, metrics, quality, releases, service_factory

app = FastAPI(
    title="Engineering Platform API",
    version="0.4.1",
    description="Control plane API for the GCP Engineering Platform. "
    "All endpoints use mock data unless GCP credentials are configured.",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # MVP: allow all. Production: restrict to platform-web origin.
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(health.router)
app.include_router(catalog.router)
app.include_router(releases.router)
app.include_router(metrics.router)
app.include_router(costs.router)
app.include_router(quality.router)
app.include_router(service_factory.router)


@app.get("/", include_in_schema=False)
async def root():
    return {
        "service": "Engineering Platform API",
        "version": "0.4.1",
        "docs": "/docs",
    }
