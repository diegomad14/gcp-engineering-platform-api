"""Pydantic models for the Engineering Platform API."""

from __future__ import annotations

from typing import Optional
from pydantic import BaseModel, Field


# ── Catalog ──────────────────────────────────────────────────────────

class CloudRunService(BaseModel):
    service_name: str
    project_id: str
    region: str


class ValidationTarget(BaseModel):
    name: str
    type: str = "external_source"
    description: str = ""


class SonarQubeProject(BaseModel):
    enabled: bool = False
    project_key: str = ""


class FinOpsLabels(BaseModel):
    app: str = ""
    env: str = ""
    owner: str = ""
    cost_center: str = ""


class Application(BaseModel):
    id: str
    name: str
    repository: str
    owner: str
    cost_center: str = ""
    release_targets: list[CloudRunService] = Field(default_factory=list)
    validation_targets: list[ValidationTarget] = Field(default_factory=list)
    quality: SonarQubeProject = Field(default_factory=SonarQubeProject)
    finops: FinOpsLabels = Field(default_factory=FinOpsLabels)


class CatalogResponse(BaseModel):
    applications: list[Application]
    total: int


# ── Releases ─────────────────────────────────────────────────────────

class ReleaseItem(BaseModel):
    app_id: str
    app_name: str
    version: str
    status: str  # candidate, promoted, rolled_back
    api_revision: str = ""
    web_revision: str = ""
    github_run_url: str = ""
    created_at: str = ""


class ServiceRevision(BaseModel):
    service_name: str  # "cgm-sanplat-api" | "cgm-sanplat-web"
    revision: str      # "cgm-sanplat-api-00173-5cs"
    action: str = "deployed"  # "deployed" | "rolled_back" | "unchanged"


class ReleaseCreateRequest(BaseModel):
    app_id: str
    app_name: str = ""
    version: str
    status: str = "candidate"  # "candidate" | "promoted" | "rolled_back"
    services: list[ServiceRevision] = Field(default_factory=list)
    github_run_url: str = ""
    triggered_by: str = "github-actions"
    rollback_from_version: str = ""
    notes: str = ""


class ReleaseSummary(BaseModel):
    recent: list[ReleaseItem] = Field(default_factory=list)
    total_releases: int = 0


# ── Quality ──────────────────────────────────────────────────────────

class QualityProject(BaseModel):
    project_key: str
    organization: str
    quality_gate_status: str = "UNKNOWN"  # OK, WARN, ERROR, UNKNOWN
    coverage: float = 0.0
    bugs: int = 0
    vulnerabilities: int = 0
    code_smells: int = 0
    url: str = ""


class QualitySummary(BaseModel):
    projects: list[QualityProject] = Field(default_factory=list)


# ── Metrics ──────────────────────────────────────────────────────────

class CloudRunServiceMetrics(BaseModel):
    service_name: str
    request_count: int = 0
    error_rate: float = 0.0
    p95_latency_ms: float = 0.0
    cpu_utilization: float = 0.0
    memory_utilization: float = 0.0
    instances_max: int = 0


class MetricsSummary(BaseModel):
    period: str = "last_24h"
    services: list[CloudRunServiceMetrics] = Field(default_factory=list)


# ── Costs ────────────────────────────────────────────────────────────

class CostItem(BaseModel):
    project_id: str = ""
    app: str = ""
    service_name: str = ""
    gcp_service: str = ""
    cost: float = 0.0
    credits: float = 0.0
    net_cost: float = 0.0


class CostPeriod(BaseModel):
    start: str
    end: str


class CostSummary(BaseModel):
    currency: str = "USD"
    period: CostPeriod
    total_cost: float = 0.0
    total_credits: float = 0.0
    total_net_cost: float = 0.0
    items: list[CostItem] = Field(default_factory=list)


# ── Service Factory ──────────────────────────────────────────────────

class ServiceFactoryRequest(BaseModel):
    app_name: str
    service_name: str
    service_type: str  # api, web, worker, integration
    runtime: str  # python, node, static
    gcp_project: str
    region: str = "us-central1"
    owner: str
    cost_center: str = ""
    environment: str = "prod"
    cloud_run_service_name: str = ""
    health_path: str = "/health"
    openapi_path: str = "/openapi.json"
    sonar_project_key: str = ""
    sonar_organization: str = ""
    validation_targets: list[str] = Field(default_factory=list)


class ServiceFactoryTemplate(BaseModel):
    name: str
    description: str
    type: str  # api, web, worker, integration


class ServiceFactoryPlan(BaseModel):
    app_name: str
    service_name: str
    generated_files: list[str] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    yaml_contract: str = ""
    caller_pr_check: str = ""
    caller_release_candidate: str = ""
    caller_promote: str = ""
    caller_rollback: str = ""
    labels_manifest: str = ""
    sonar_properties: str = ""


# ── Health ────────────────────────────────────────────────────────────

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    mock_mode: bool = True


class ServiceHealthItem(BaseModel):
    app_id: str
    app_name: str
    service_name: str
    project_id: str
    region: str
    status: str = "unknown"
    checked_at: str = ""
    error: str = ""


class ServicesHealthResponse(BaseModel):
    status: str = "ok"
    services: list[ServiceHealthItem] = Field(default_factory=list)
