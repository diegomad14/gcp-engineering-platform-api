"""Canonical, server-owned quality profiles for all managed services."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import config
from ..models import CatalogService


def _load_document() -> dict[str, Any]:
    path = Path(__file__).resolve().parent.parent / "release_quality_profiles.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema_version") != 1 or not isinstance(value.get("profiles"), dict):
        raise RuntimeError("Release quality profile document is invalid")
    return value


_DOCUMENT = _load_document()


@dataclass(frozen=True)
class QualityProfile:
    name: str
    spec: dict[str, Any]

    @property
    def runtime(self) -> str:
        return str(self.spec["runtime"])

    @property
    def coverage_threshold(self) -> int:
        return int(self.spec["coverage_threshold"])

    @property
    def timeout_seconds(self) -> int:
        return int(self.spec["timeout_seconds"])

    @property
    def special_checks(self) -> tuple[str, ...]:
        values = [str(item["name"]) for item in self.spec.get("extra", [])]
        if self.spec.get("postgres"):
            values.append("postgres")
        if self.spec.get("container_smoke"):
            values.append("container-smoke")
        return tuple(values)

    def fingerprint(self) -> str:
        payload = {
            "schema_version": _DOCUMENT["schema_version"],
            "service_name": self.name,
            "profile": self.spec,
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()


_PROFILES = {
    name: QualityProfile(name=name, spec=spec)
    for name, spec in _DOCUMENT["profiles"].items()
}


def profile_for(service: CatalogService) -> QualityProfile:
    try:
        profile = _PROFILES[service.service_name]
    except KeyError as exc:
        raise ValueError(
            f"No quality profile is defined for {service.service_name!r}"
        ) from exc
    if service.quality.profile and service.quality.profile != profile.runtime:
        raise ValueError("Catalog and quality profile runtime do not match")
    if (
        service.quality.coverage_threshold is not None
        and int(service.quality.coverage_threshold) != profile.coverage_threshold
    ):
        raise ValueError("Catalog and quality profile coverage threshold do not match")
    return profile


def executor_image(profile: QualityProfile) -> str:
    image = (
        config.release_orchestrator.quality_node_image
        if profile.runtime == "node"
        else config.release_orchestrator.quality_python_image
    )
    if "@sha256:" not in image:
        raise ValueError("Quality executor image must be pinned by digest")
    return image


def planner_hash() -> str:
    """Bind executions to the immutable planner plus semantic package policy."""
    image = config.release_orchestrator.release_planner_image
    if "@sha256:" not in image:
        raise ValueError("Release planner image must be pinned by digest")
    value = {
        "image": image,
        "commit_analyzer": "13.0.1",
        "release_notes_generator": "14.1.1",
        "tag_format": "v${version}",
        "branch": "main",
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
