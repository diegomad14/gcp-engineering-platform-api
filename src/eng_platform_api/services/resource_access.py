"""Management barriers for inventory-only resources, independent of log access.

This is a negative capability check, not authentication or deployment approval.
Existing managed-resource authorization and readiness checks still apply. Read
the local authority again so a stale operation/object cannot restore privileges.
"""

from typing import cast

from fastapi import HTTPException

from ..models import CatalogService
from . import log_catalog


class ManagedCatalogService(CatalogService):
    """Type refinement after the management barrier; never a public DTO."""

    repository: str
    owner: str


def require_managed_service_name(service_name: str) -> None:
    """Reject observed identities before any provider or persistence operation.

    Unregistered names retain their existing caller-specific handling. This helper
    does not grant them authorization, infer ownership or turn them into services.
    """
    try:
        records = log_catalog.load_catalog()
    except log_catalog.CatalogUnavailable:
        raise HTTPException(503, "Runtime catalog is unavailable") from None
    if any(
        item["service_name"] == service_name
        and item.get("management_mode", "managed") == "observability_only"
        for item in records
    ):
        raise HTTPException(409, "Resource is observation-only; management is disabled")


def require_managed(service: CatalogService) -> ManagedCatalogService:
    """Reject observation-only metadata, including an untrusted/stale object."""
    if service.management_mode != "managed":
        raise HTTPException(409, "Resource is observation-only; management is disabled")
    require_managed_service_name(service.service_name)
    if service.repository is None or service.owner is None:
        raise HTTPException(409, "Resource management metadata is incomplete")
    return cast(ManagedCatalogService, service)
