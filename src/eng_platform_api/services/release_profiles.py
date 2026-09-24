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
    candidate_env_vars: tuple[tuple[str, str], ...] = ()
    pre_candidate_hooks: tuple[str, ...] = ()
    candidate_hooks: tuple[str, ...] = ()
    pre_promote_hooks: tuple[str, ...] = ()
    post_promote_hooks: tuple[str, ...] = ()
    recovery_hook: str = ""
    rollback_hook: str = ""
    candidate_update_strategy: str = "merge"
    rollback_mode: str = "traffic"

    def fingerprint(self) -> str:
        value = asdict(self)
        if self.name in {
            "eng-platform-api",
            "eng-platform-web",
            "communications-ms",
        }:
            # These three profiles have no new paired behavior. Keep their
            # pre-upgrade hash so the production executor remains usable while
            # the API revision and then the pinned executor digest roll out.
            value = {
                "name": self.name,
                "timeout_seconds": self.timeout_seconds,
                "build_args": self.build_args,
                "hooks": self.candidate_hooks,
                "candidate_update_strategy": self.candidate_update_strategy,
                "rollback_mode": self.rollback_mode,
            }
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


_PROFILES: dict[str, ReleaseProfile] = {
    "eng-platform-api": ReleaseProfile(
        "eng-platform-api", 1800, candidate_hooks=("verify_candidate_config",)
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
        pre_candidate_hooks=("ensure_bulk_queue", "deploy_bulk_worker"),
        candidate_hooks=("validate_smarti",),
        post_promote_hooks=("promote_bulk_worker",),
        recovery_hook="recover_bulk_worker",
        rollback_hook="rollback_bulk_worker",
        candidate_update_strategy="overwrite",
    ),
    "cgm-sanplat-web": ReleaseProfile(
        "cgm-sanplat-web",
        3600,
        pre_promote_hooks=("corporate_window_web",),
        post_promote_hooks=("wait_corporate_activation_web",),
        recovery_hook="recover_corporate_web",
        rollback_hook="rollback_corporate_web",
        rollback_mode="corporate_web",
    ),
    "cgm-sanplat-api": ReleaseProfile(
        "cgm-sanplat-api",
        3600,
        candidate_env_vars=(("APP_RELEASE_SHA", "{sha}"),),
        pre_candidate_hooks=("prepare_corporate_runtimes",),
        candidate_hooks=("validate_openapi_inventory", "validate_corporate_auth"),
        pre_promote_hooks=("corporate_window_api",),
        post_promote_hooks=("wait_corporate_activation_api",),
        recovery_hook="recover_corporate_api",
        rollback_hook="rollback_corporate_api",
        rollback_mode="corporate_api",
    ),
}

_ARTEMIS_SERVICES = (
    "cgm-artemis-api",
    "cgm-artemis-web",
    "cgm-artemis-job-dispatcher",
    "cgm-artemis-job-worker",
    "cgm-artemis-sync-worker",
    "cgm-artemis-clock-sync-worker",
    "cgm-artemis-data-recovery-worker",
    "cgm-artemis-fnd-ip-sync-worker",
)
_ARTEMIS_JOBS = (
    "cgm-artemis-fnd-observation-worker",
    "cgm-artemis-readings-export-worker",
    "cgm-artemis-smarti-prevention-worker",
    "cgm-artemis-wm-sweep-worker",
)
for _name in _ARTEMIS_SERVICES:
    _PROFILES[_name] = ReleaseProfile(
        _name,
        3600,
        candidate_env_vars=(("APP_RELEASE_SHA", "{sha}"),)
        if _name != "cgm-artemis-web"
        else (),
    )
for _name in _ARTEMIS_JOBS:
    _PROFILES[_name] = ReleaseProfile(
        _name,
        3600,
        candidate_env_vars=(("APP_RELEASE_SHA", "{sha}"),),
        rollback_mode="job_definition",
    )


def profile_for(service: CatalogService) -> ReleaseProfile:
    """Return the only profile permitted for a catalogued service."""
    try:
        return _PROFILES[service.service_name]
    except KeyError as exc:
        raise ValueError(
            f"No central release profile is defined for {service.service_name!r}"
        ) from exc
