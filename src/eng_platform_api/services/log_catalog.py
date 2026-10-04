"""Validated local catalog authority, shared by metadata and log authorization.

No discovery, Cloud Run readiness or provider clients belong in this module.
Production log access requires a server-configured, digest-pinned local mount.
The packaged JSON is a closed fixture; onboarding YAML is only a proposal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from ..config import config, validate_catalog_source
from ..models import (
    FinOpsLabels,
    InventorySource,
    OperationalSecret,
    ServiceDeploymentConfig,
    ServiceQualityConfig,
    ValidationTarget,
)

CATALOG_PATH = (
    Path(__file__).resolve().parent.parent / "static_examples/mock_catalog.json"
)
MAX_CATALOG_BYTES = 4 * 1024 * 1024
MAX_RESOURCES = 2048
MAX_PROJECTS = MAX_RESOURCES
PROJECT_PATTERN = r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$"
REGION_PATTERN = r"^[a-z]+(?:-[a-z]+)+[0-9]+$"
RESOURCE_PATTERN = r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$|^[a-z]$"
LOGIN_PATTERN = r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$"
RuntimeKind = Literal["cloud_run_service", "cloud_run_job"]
Login = Annotated[str, Field(pattern=LOGIN_PATTERN, max_length=39)]


class CatalogUnavailable(ValueError):
    """The authority cannot be trusted; never use a partial or previous snapshot."""


class LogPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    # Both keys are mandatory when a policy block is present. An omitted block
    # is still denied by default and is distinguishable in the public capability.
    enabled: bool
    allowed_logins: list[Login] = Field(max_length=512)

    @field_validator("allowed_logins")
    @classmethod
    def canonical_logins(cls, values: list[str]) -> list[str]:
        canonical = [value.lower() for value in values]
        if len(set(canonical)) != len(canonical):
            raise ValueError("Duplicate log reader")
        return sorted(canonical)


class CatalogQuality(ServiceQualityConfig):
    model_config = ConfigDict(extra="forbid", strict=True)
    coverage_threshold: float = Field(default=70, ge=0, le=100)
    differential_threshold: float = Field(default=80, ge=80, le=100)
    policy_version: Literal["oss-v1", "oss-v2"] = "oss-v2"


class CatalogDeployment(ServiceDeploymentConfig):
    model_config = ConfigDict(extra="forbid", strict=True)
    runtime_kind: RuntimeKind


class CatalogFinOps(FinOpsLabels):
    model_config = ConfigDict(extra="forbid", strict=True)


class CatalogValidationTarget(ValidationTarget):
    model_config = ConfigDict(extra="forbid", strict=True)


class CatalogOperationalSecret(OperationalSecret):
    model_config = ConfigDict(extra="forbid", strict=True)


class CatalogRecord(BaseModel):
    """Private on-disk contract. Never serialize this as a public API model."""

    model_config = ConfigDict(extra="forbid", strict=True)
    management_mode: Literal["managed"] = "managed"
    service_name: str = Field(pattern=RESOURCE_PATTERN, max_length=63)
    repository: str = Field(min_length=1, max_length=256)
    owner: str = Field(min_length=1, max_length=128)
    cost_center: str = ""
    project_id: str = Field(pattern=PROJECT_PATTERN, max_length=30)
    region: str = Field(pattern=REGION_PATTERN, max_length=63)
    environment: str = "prod"
    release_model: str = "managed-release"
    release_policy: str = "oss-v2"
    validation_targets: list[CatalogValidationTarget] = Field(default_factory=list)
    quality: CatalogQuality = Field(default_factory=CatalogQuality)
    deployment: CatalogDeployment
    finops: CatalogFinOps = Field(default_factory=CatalogFinOps)
    operational_secrets: list[CatalogOperationalSecret] = Field(default_factory=list)
    logs: LogPolicy = Field(
        default_factory=lambda: LogPolicy(enabled=False, allowed_logins=[])
    )


class CatalogInventorySource(InventorySource):
    model_config = ConfigDict(extra="forbid", strict=True)
    observed_at: str = Field(max_length=64)
    project: str = Field(pattern=PROJECT_PATTERN, max_length=30)
    region: str = Field(pattern=REGION_PATTERN, max_length=63)

    @field_validator("observed_at")
    @classmethod
    def timestamp_with_timezone(cls, value: str) -> str:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("Inventory timestamp needs a timezone")
        return value


class ObservationDeployment(CatalogDeployment):
    enabled: Literal[False] = False
    executor: Literal["auto"] = "auto"
    workflow_file: Literal[""] = ""
    image_name: Literal[""] = ""
    artifact_repository: Literal[""] = ""
    build_context: Literal[""] = ""
    dockerfile_path: Literal[""] = ""
    health_path: Literal[""] = ""
    api_base_url: Literal[""] = ""
    api_candidate_base_url: Literal[""] = ""

    @field_validator("enabled", mode="before")
    @classmethod
    def strictly_disabled(cls, value):
        if value is not False:
            raise ValueError("Observation-only deployment must be false")
        return value


class ObservationQuality(CatalogQuality):
    enabled: Literal[False] = False
    profile: None = None
    evidence_services: list[str] = Field(default_factory=list, max_length=0)

    @field_validator("enabled", mode="before")
    @classmethod
    def strictly_disabled(cls, value):
        if value is not False:
            raise ValueError("Observation-only quality must be false")
        return value


class ObservationFinOps(CatalogFinOps):
    service: Literal[""] = ""
    env: Literal[""] = ""
    owner: Literal[""] = ""
    cost_center: Literal[""] = ""


class ObservabilityRecord(BaseModel):
    """Inventory identity only; no repository/owner or mutation authority inferred."""

    model_config = ConfigDict(extra="forbid", strict=True)
    management_mode: Literal["observability_only"]
    service_name: str = Field(pattern=RESOURCE_PATTERN, max_length=63)
    repository: None
    owner: None
    cost_center: Literal[""] = ""
    project_id: str = Field(pattern=PROJECT_PATTERN, max_length=30)
    region: str = Field(pattern=REGION_PATTERN, max_length=63)
    environment: Literal[""] = ""
    release_model: Literal["observability-only"] = "observability-only"
    release_policy: Literal[""] = ""
    validation_targets: list[CatalogValidationTarget] = Field(
        default_factory=list, max_length=0
    )
    quality: ObservationQuality = Field(default_factory=ObservationQuality)
    deployment: ObservationDeployment
    finops: ObservationFinOps = Field(default_factory=ObservationFinOps)
    operational_secrets: list[CatalogOperationalSecret] = Field(
        default_factory=list, max_length=0
    )
    inventory_source: CatalogInventorySource
    logs: LogPolicy = Field(
        default_factory=lambda: LogPolicy(enabled=False, allowed_logins=[])
    )

    @model_validator(mode="after")
    def inventory_coordinates_match(self):
        if (
            self.project_id != self.inventory_source.project
            or self.region != self.inventory_source.region
        ):
            raise ValueError("Inventory coordinates do not match resource")
        return self


CatalogEntry = Annotated[
    CatalogRecord | ObservabilityRecord, Field(discriminator="management_mode")
]


class CatalogDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    services: list[CatalogEntry] = Field(min_length=1, max_length=MAX_RESOURCES)

    @field_validator("services", mode="before")
    @classmethod
    def legacy_managed_entries(cls, values):
        # Legacy managed proposals remain accepted only when their existing
        # mandatory owner/repository and explicit runtime coordinates validate.
        if not isinstance(values, list):
            return values
        return [
            {"management_mode": "managed", **item} if isinstance(item, dict) else item
            for item in values
        ]


def validate_catalog(data: object) -> list[dict]:
    """Validate the complete snapshot before any resource can be used.

    Preserve omitted optional fields for callers and reviewers. Validation does
    not infer coordinates, enable logs, or apply historical deployment defaults
    to the trusted source. Names remain globally unique for existing API routes.
    """
    try:
        document = CatalogDocument.model_validate(data)
    except (ValidationError, ValueError, TypeError):
        raise CatalogUnavailable("Catalog validation failed") from None
    names = [item.service_name for item in document.services]
    if len(names) != len(set(names)):
        raise CatalogUnavailable("Duplicate catalog service_name")
    if len({item.project_id for item in document.services}) > MAX_PROJECTS:
        raise CatalogUnavailable("Catalog project limit exceeded")
    return [item.model_dump(exclude_unset=True) for item in document.services]


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise CatalogUnavailable("Duplicate catalog field")
        value[key] = item
    return value


def _invalid_constant(_value: str) -> None:
    raise CatalogUnavailable("Invalid catalog JSON constant")


def load_catalog(path: Path | None = None) -> list[dict]:
    """Read one bounded, current authority; never fall back on source failures.

    No request controls the source. An explicit path is retained exclusively for
    offline registry validation; runtime callers always use server configuration.
    """
    try:
        expected_sha256 = None
        if path is None:
            validate_catalog_source(
                config.catalog_path,
                config.catalog_sha256,
                required=not config.mock_mode,
            )
            if config.catalog_path is not None:
                path = Path(config.catalog_path)
                expected_sha256 = config.catalog_sha256
            else:
                path = CATALOG_PATH
        # A malformed mount must not block on a FIFO or read an unbounded device.
        # Inspect the opened descriptor, so replacing a path cannot evade this.
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise CatalogUnavailable("Catalog source is not a regular file")
            raw = source.read(MAX_CATALOG_BYTES + 1)
        if len(raw) > MAX_CATALOG_BYTES:
            raise CatalogUnavailable("Catalog file limit exceeded")
        if (
            expected_sha256 is not None
            and hashlib.sha256(raw).hexdigest() != expected_sha256
        ):
            raise CatalogUnavailable("Catalog integrity check failed")
        data = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_constant,
        )
        return validate_catalog(data)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        # Never expose paths, identities, policy contents or parser diagnostics.
        raise CatalogUnavailable("Runtime catalog is unavailable") from None


@dataclass(frozen=True)
class Resource:
    service_id: str
    project: str
    region: str
    kind: str
    enabled: bool = False
    allowed_logins: tuple[str, ...] = ()

    @property
    def resource_type(self) -> str:
        return "cloud_run_job" if self.kind == "cloud_run_job" else "cloud_run_revision"

    @property
    def name_label(self) -> str:
        return "job_name" if self.kind == "cloud_run_job" else "service_name"

    @property
    def key(self) -> tuple[str, str, str, str]:
        return self.project, self.region, self.kind, self.service_id

    @property
    def policy_fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                [
                    "resource-log-policy-v1",
                    self.key,
                    self.enabled,
                    sorted(self.allowed_logins),
                ],
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def can_read(self, login: str) -> bool:
        return bool(
            self.enabled is True
            and isinstance(login, str)
            and login
            and login.lower() in self.allowed_logins
        )


def resources() -> list[Resource]:
    """Return the entire validated authority without invoking cloud clients."""
    return [
        Resource(
            item["service_name"],
            item["project_id"],
            item["region"],
            item["deployment"]["runtime_kind"],
            item.get("logs", {}).get("enabled", False),
            tuple(item.get("logs", {}).get("allowed_logins", [])),
        )
        for item in load_catalog()
    ]
