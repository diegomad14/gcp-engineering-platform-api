"""Server-owned, default-off adoption policy for individual local releases."""

from ..config import config
from . import deployment_store


class LocalReleasePolicyError(ValueError):
    """An executor is not permitted for this destination."""


def adopted(service_name: str) -> bool:
    # Keep legacy dispatch blocked even while local execution is temporarily off.
    return service_name in config.release_execution.allowed_services


def require_legacy_allowed(service_name: str) -> None:
    if adopted(service_name):
        raise LocalReleasePolicyError(
            "This service uses the local release executor; Actions dispatch is disabled"
        )


def require_enabled(service_name: str) -> None:
    if not config.release_execution.remote_activation_enabled or not adopted(
        service_name
    ):
        raise LocalReleasePolicyError(
            "Local release execution is disabled for this service"
        )
    if config.mock_mode:
        return
    if not (
        config.release_execution.firestore_collection
        and config.monitoring.gcp_project_id
        and config.github.release_authorization_collection
        and config.github.release_signing_private_key
        and config.auth.allowed_logins
    ):
        raise LocalReleasePolicyError(
            "Durable authenticated local release control is not configured"
        )
    # A deployment already dispatched before adoption may still have effects.
    # Never assume that switching policy cancels or finishes that work.
    terminal = {"SUCCEEDED", "FAILED", "CANCELLED", "ROLLED_BACK", "ROLLBACK_FAILED"}
    if any(
        item.status not in terminal
        for item in deployment_store.list_for_service(service_name, limit=1000)
    ):
        raise LocalReleasePolicyError(
            "An existing deployment must finish before local execution"
        )
