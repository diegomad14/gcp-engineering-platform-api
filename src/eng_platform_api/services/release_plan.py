"""Trusted, immutable release plans. No secrets or user-selected destinations."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from google.cloud import firestore

from ..models import CatalogService, DeploymentItem, ReleaseTag
from . import operational_secrets


class ReleasePlanError(ValueError):
    """A release prerequisite is not satisfied."""


def execution_repository() -> str:
    value = os.getenv("ENG_PLATFORM_EXECUTION_REPOSITORY", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        raise ReleasePlanError("Central execution repository is not configured")
    return value


def enabled(service_name: str) -> bool:
    return service_name in {
        name.strip()
        for name in os.getenv("ENG_PLATFORM_CENTRAL_SERVICES", "").split(",")
        if name.strip()
    }


def digest(plan: dict) -> str:
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def require_quality(repo, sha: str) -> None:
    """Require fresh successful checks on the exact commit, not a tag label."""
    latest: dict[str, Any] = {}
    for check in repo.get_commit(sha).get_check_runs():
        previous = latest.get(check.name)
        if previous is None or check.id > previous.id:
            latest[check.name] = check
    quality = [
        latest[name] for name in ("quality", "quality / quality-gate") if name in latest
    ]
    sonar = latest.get("SonarCloud Code Analysis")
    if not quality or sonar is None:
        raise ReleasePlanError("Exact commit has no complete quality evidence")
    if any(
        check.status != "completed" or check.conclusion != "success"
        for check in [*quality, sonar]
    ):
        raise ReleasePlanError("Exact commit quality gate is not successful")
    if any(
        check.conclusion in {"failure", "cancelled", "timed_out", "action_required"}
        for check in latest.values()
    ):
        raise ReleasePlanError("Exact commit has a failed active check")


def register_immutable_tag(db, repository: str, tag: ReleaseTag) -> None:
    ref = db.collection("eng_platform_release_tags").document(
        hashlib.sha256(f"{repository}:{tag.name}".encode()).hexdigest()
    )

    @firestore.transactional
    def commit(transaction):
        previous = ref.get(transaction=transaction).to_dict()
        if previous and previous["sha"] != tag.sha:
            raise ReleasePlanError("Release tag moved since registration")
        if not previous:
            transaction.create(
                ref,
                {
                    "repository": repository,
                    "tag": tag.name,
                    "sha": tag.sha,
                    "registered_at": datetime.now(timezone.utc).isoformat(),
                },
            )

    commit(db.transaction())


def runner(repo, requested: str) -> str:
    configured = os.getenv("CGM_ACTIONS_RUNNER", "")
    if configured not in {"", "cgm-release-local"} or requested not in {
        "",
        "cgm-release-local",
    }:
        raise ReleasePlanError("Unsupported runner configuration")
    if not requested:
        return "ubuntu-latest"
    if configured != requested:
        raise ReleasePlanError("Fallback runner is not enabled")
    for candidate in repo.get_self_hosted_runners():
        labels = {label.name for label in candidate.labels}
        if candidate.status == "online" and not candidate.busy and requested in labels:
            return requested
    raise ReleasePlanError("Fallback runner is not online and available")


def create(
    service: CatalogService,
    tag: ReleaseTag,
    repo,
    db,
    target: DeploymentItem | None = None,
) -> dict:
    fresh_sha = repo.get_commit(tag.name).sha
    if fresh_sha != tag.sha or not re.fullmatch(r"[0-9a-f]{40}", tag.sha):
        raise ReleasePlanError("Release tag moved since selection")
    require_quality(repo, tag.sha)
    register_immutable_tag(db, service.repository, tag)
    configuration = (
        target.configuration if target else operational_secrets.snapshot(service)
    )
    if target and (not target.image_digest or not target.runtime_snapshot):
        raise ReleasePlanError("Rollback target has no immutable runtime configuration")
    plan = {
        "service_name": service.service_name,
        "repository": service.repository,
        "sha": tag.sha,
        "tag": tag.name,
        "project_id": service.project_id,
        "region": service.region,
        "image_name": service.deployment.image_name or service.service_name,
        "artifact_repository": service.deployment.artifact_repository,
        "build_context": service.deployment.build_context,
        "health_path": service.deployment.health_path,
        "configuration": configuration,
        "auxiliary_services": service.deployment.auxiliary_services,
        "auxiliary_jobs": service.deployment.auxiliary_jobs,
        "kind": "rollback" if target else "deploy",
        "target_revision": target.production_revision if target else "",
        "target_digest": target.image_digest if target else "",
        "target_runtimes": target.runtime_snapshot if target else {},
    }
    return plan
