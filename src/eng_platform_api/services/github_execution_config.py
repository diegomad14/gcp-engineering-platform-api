"""Server-owned engine context for the independent Artemis GitHub runtimes."""

from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from ..config import config
from ..models import CatalogService, DeploymentItem
from . import deployment_executions
from .repository_identity import aliases

_FIELDS = {
    "evidence_bucket",
    "runtime_kind",
    "private_runtime",
    "dockerfile_path",
    "quality_evidence_services",
    "repository_aliases",
    "paired_web_sha",
}


def requires_execution_config(service: CatalogService) -> bool:
    return service.service_name.startswith("cgm-artemis-")


def _validate(payload: object, service_name: str) -> dict:
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ValueError("GitHub engine configuration requires reconciliation")
    bucket = payload["evidence_bucket"]
    path = payload["dockerfile_path"]
    paired = payload["paired_web_sha"]
    if (
        not isinstance(bucket, str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,222}", bucket)
        or not isinstance(payload["runtime_kind"], str)
        or payload["runtime_kind"] not in {"cloud_run_service", "cloud_run_job"}
        or type(payload["private_runtime"]) is not bool
        or not isinstance(path, str)
        or not re.fullmatch(r"[A-Za-z0-9_./-]+", path)
        or PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        or not isinstance(paired, str)
        or (paired and not re.fullmatch(r"[0-9a-f]{40}", paired))
        or (service_name == "cgm-artemis-api" and not paired)
    ):
        raise ValueError("GitHub Artemis engine configuration is incomplete or invalid")
    for field, pattern in (
        ("quality_evidence_services", r"[a-z][a-z0-9-]*"),
        ("repository_aliases", r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"),
    ):
        values = payload[field]
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not re.fullmatch(pattern, value)
            for value in values
        ):
            raise ValueError("GitHub engine evidence identity is invalid")
    return payload


def execution_config_for(service: CatalogService, item: DeploymentItem) -> str:
    """Reuse a bound snapshot; catalog/configuration are never request inputs."""
    if not requires_execution_config(service):
        return ""
    if not service.repository:
        raise ValueError("GitHub engine configuration requires a managed repository")
    execution = deployment_executions.get(item.id) or {}
    stored = execution.get("github_execution_config")
    if stored:
        _validate(json.loads(stored), service.service_name)
        return str(stored)
    payload = {
        "evidence_bucket": config.github.deployment_evidence_bucket,
        "runtime_kind": service.deployment.runtime_kind,
        "private_runtime": service.deployment.private_runtime,
        "dockerfile_path": service.deployment.dockerfile_path,
        "quality_evidence_services": list(service.quality.evidence_services),
        "repository_aliases": list(aliases(service.repository)),
        "paired_web_sha": config.release_orchestrator.artemis_web_sha,
    }
    return json.dumps(
        _validate(payload, service.service_name), sort_keys=True, separators=(",", ":")
    )
