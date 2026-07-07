"""Quality router — SonarQube Cloud status."""

from fastapi import APIRouter

from ..models import QualitySummary
from ..services import sonarqube

router = APIRouter(prefix="/api/quality", tags=["quality"])


@router.get("/summary", response_model=QualitySummary)
async def get_quality_summary():
    """Get SonarQube quality status across all projects."""
    return sonarqube.get_quality_summary()
