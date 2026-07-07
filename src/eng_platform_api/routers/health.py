"""Health check router."""

from fastapi import APIRouter

from ..config import config
from ..models import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health_check():
    """Platform API health check."""
    return HealthResponse(
        status="ok",
        version="0.1.0",
        mock_mode=config.mock_mode,
    )
