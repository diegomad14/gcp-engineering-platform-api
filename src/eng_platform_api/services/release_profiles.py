"""Central, allowlisted release profiles shared by both executors.

The catalog owns service identity.  This module owns the operational sequence;
callers cannot provide shell fragments, machine types, secrets or hooks.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass

from ..models import CatalogService


@dataclass(frozen=True)
class ReleaseProfile:
    name: str
    timeout_seconds: int
    build_args: tuple[tuple[str, str], ...] = ()
    hooks: tuple[str, ...] = ()
    candidate_update_strategy: str = "merge"
    rollback_mode: str = "traffic"

    def fingerprint(self) -> str:
        payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


_PROFILES: dict[str, ReleaseProfile] = {
    "eng-platform-api": ReleaseProfile(
        "eng-platform-api", 1800, hooks=("verify_candidate_config",)
    ),
    "eng-platform-web": ReleaseProfile(
        "eng-platform-web", 1800, build_args=(("APP_VERSION", "{tag}"),)
    ),
    "communications-ms": ReleaseProfile(
        "communications-ms", 1800, candidate_update_strategy="overwrite"
    ),
    "cgm-bot-api": ReleaseProfile(
        "cgm-bot-api",
        1800,
        hooks=("ensure_bulk_queue", "deploy_bulk_worker", "validate_smarti"),
        candidate_update_strategy="overwrite",
    ),
    "cgm-sanplat-web": ReleaseProfile(
        "cgm-sanplat-web",
        3600,
        hooks=("corporate_window_web", "wait_corporate_activation_web"),
        rollback_mode="corporate_web",
    ),
    "cgm-sanplat-api": ReleaseProfile(
        "cgm-sanplat-api",
        3600,
        hooks=(
            "prepare_corporate_runtimes",
            "deploy_external_jobs",
            "deploy_smarti_prevention",
            "validate_corporate_runtimes",
            "validate_wm_perseo",
            "validate_openapi_inventory",
            "validate_corporate_auth",
            "corporate_window_api",
            "activate_job_engine",
            "wait_corporate_activation_api",
        ),
        rollback_mode="corporate_api",
    ),
}


def profile_for(service: CatalogService) -> ReleaseProfile:
    """Return the only profile permitted for a catalogued service."""
    try:
        return _PROFILES[service.service_name]
    except KeyError as exc:
        raise ValueError(
            f"No central release profile is defined for {service.service_name!r}"
        ) from exc
