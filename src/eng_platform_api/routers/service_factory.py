"""Service Factory router — generate onboarding artifacts for new services."""

from fastapi import APIRouter

from ..models import ServiceFactoryPlan, ServiceFactoryRequest, ServiceFactoryTemplate
from ..services import service_factory as sf

router = APIRouter(prefix="/api/service-factory", tags=["service-factory"])


@router.get("/templates", response_model=list[ServiceFactoryTemplate])
async def list_templates():
    """List available service templates."""
    return sf.get_templates()


@router.post("/plan", response_model=ServiceFactoryPlan)
async def generate_plan(request: ServiceFactoryRequest):
    """Generate an onboarding plan for a new service.

    This creates YAML contracts, caller workflow files, and a checklist.
    It does NOT create any GCP resources, IAM bindings, or secrets.
    """
    return sf.generate_plan(request)
