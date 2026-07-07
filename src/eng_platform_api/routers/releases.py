"""Releases router — release history and status."""

from fastapi import APIRouter

from ..models import ReleaseSummary
from ..services import github_actions

router = APIRouter(prefix="/api/releases", tags=["releases"])


@router.get("/summary", response_model=ReleaseSummary)
async def get_release_summary():
    """Get recent release activity across all applications."""
    return github_actions.get_release_summary()
