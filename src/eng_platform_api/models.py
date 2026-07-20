"""Pydantic models for the Engineering Platform API."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field


# ── Catalog ──────────────────────────────────────────────────────────


class ValidationTarget(BaseModel):
    name: str
    type: str = "external_source"
    description: str = ""


class ServiceQualityConfig(BaseModel):
    enabled: bool = False
    profile: Literal["python", "node", "static"] | None = None
    coverage_threshold: float = 70.0


class ServiceDeploymentConfig(BaseModel):
    enabled: bool = True
    workflow_file: str = "platform-deploy.yml"
    image_name: str = ""
    artifact_repository: str = "cgm-sanplat-repo"
    build_context: str = "."
    health_path: str = "/"


class FinOpsLabels(BaseModel):
    service: str = ""
    env: str = ""
    owner: str = ""
    cost_center: str = ""


class CatalogService(BaseModel):
    service_name: str
    repository: str
    owner: str
    cost_center: str = ""
    project_id: str
    region: str
    environment: str = "prod"
    validation_targets: list[ValidationTarget] = Field(default_factory=list)
    quality: ServiceQualityConfig = Field(default_factory=ServiceQualityConfig)
    deployment: ServiceDeploymentConfig = Field(default_factory=ServiceDeploymentConfig)
    finops: FinOpsLabels = Field(default_factory=FinOpsLabels)
    deployment_ready: bool = False
    deployment_blockers: list[str] = Field(default_factory=list)


class ServiceTraffic(BaseModel):
    revision: str = ""
    percent: int = 0
    tag: str = ""


class ServiceDetail(CatalogService):
    status: str = "unknown"
    url: str = ""
    latest_ready_revision: str = ""
    traffic: list[ServiceTraffic] = Field(default_factory=list)
    error: str = ""


class CatalogResponse(BaseModel):
    services: list[CatalogService]
    total: int


# ── Releases ─────────────────────────────────────────────────────────

ReleaseServiceAction = Literal[
    "released",
    "promoted",
    "deployed",
    "rolled_back",
    "unchanged",
    "not_included",
    "missing",
]


class ServiceRevision(BaseModel):
    service_name: str
    revision: str = ""
    action: ReleaseServiceAction = "deployed"


class ReleaseItem(BaseModel):
    service_name: str
    repository: str
    version: str
    status: str  # candidate, promoted, rolled_back
    revision: str = ""
    action: ReleaseServiceAction = "deployed"
    github_run_url: str = ""
    created_at: str = ""


class ReleaseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    version: str
    status: str = "candidate"  # "candidate" | "promoted" | "rolled_back"
    services: list[ServiceRevision] = Field(min_length=1)
    github_run_url: str = ""
    triggered_by: str = "github-actions"
    rollback_from_version: str = ""
    notes: str = ""


class ReleaseSummary(BaseModel):
    recent: list[ReleaseItem] = Field(default_factory=list)
    total_releases: int = 0


# ── GitHub-native deployments ──────────────────────────────────────────

DeploymentStatus = Literal[
    "QUEUED",
    "VERIFYING_RELEASE",
    "BUILDING",
    "DEPLOYING_CANDIDATE",
    "VALIDATING_CANDIDATE",
    "PROMOTING",
    "VALIDATING_PRODUCTION",
    "SUCCEEDED",
    "FAILED",
    "ROLLING_BACK",
    "ROLLED_BACK",
    "ROLLBACK_FAILED",
]
DeploymentStageStatus = Literal["pending", "running", "succeeded", "failed", "skipped"]


class ReleaseTag(BaseModel):
    name: str
    sha: str
    created_at: str = ""
    url: str = ""
    eligible: bool = True
    reason: str = ""


class ReleaseTagPage(BaseModel):
    items: list[ReleaseTag] = Field(default_factory=list)
    next_cursor: str | None = None


class DeploymentStage(BaseModel):
    key: str
    label: str
    status: DeploymentStageStatus = "pending"
    started_at: str = ""
    completed_at: str = ""
    duration_seconds: float | None = None
    details: str = ""


class DeploymentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tag: str = Field(min_length=1, max_length=128)


class DeploymentItem(BaseModel):
    id: str
    service_name: str
    repository: str
    tag: str
    sha: str = ""
    kind: Literal["deploy", "rollback"] = "deploy"
    status: DeploymentStatus = "QUEUED"
    current_stage: str = "queued"
    stages: list[DeploymentStage] = Field(default_factory=list)
    candidate_revision: str = ""
    production_revision: str = ""
    candidate_url: str = ""
    production_url: str = ""
    requested_by: str = "anonymous"
    created_at: str = ""
    updated_at: str = ""
    github_deployment_id: int | None = None
    github_run_id: int | None = None
    github_run_url: str = ""
    logs_url: str = ""
    error: str = ""


class DeploymentList(BaseModel):
    items: list[DeploymentItem] = Field(default_factory=list)
    total: int = 0


class AuthSession(BaseModel):
    authenticated: bool = False
    can_deploy: bool = False
    login: str = ""
    avatar_url: str = ""


# ── Quality ──────────────────────────────────────────────────────────

QualityGateStatus = Literal["PASSED", "FAILED", "RUNNING", "STALE", "NOT_CONFIGURED"]
QualityCheckStatus = Literal["PASSED", "FAILED", "SKIPPED"]


class QualityCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    category: str
    status: QualityCheckStatus
    findings: int = 0
    blocking_findings: int = 0
    duration_seconds: float = 0.0
    details: str = ""
    report_path: str = ""


class QualityReportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service_name: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$",
    )
    repository: str = Field(min_length=1, max_length=256)
    commit_sha: str = Field(min_length=7, max_length=64, pattern=r"^[0-9a-fA-F]+$")
    branch: str = ""
    profile: Literal["python", "node", "static"]
    workflow_run_url: str = ""
    generated_at: str
    coverage: float | None = Field(default=None, ge=0, le=100)
    coverage_threshold: float | None = Field(default=None, ge=0, le=100)
    tool_versions: dict[str, str] = Field(default_factory=dict)
    checks: list[QualityCheck] = Field(min_length=1)


class QualityReport(QualityReportCreate):
    quality_gate_status: QualityGateStatus
    received_at: str


class QualityProject(BaseModel):
    project_key: str
    organization: str = ""
    service_name: str = ""
    repository: str = ""
    commit_sha: str = ""
    branch: str = ""
    profile: str = ""
    quality_gate_status: QualityGateStatus = "NOT_CONFIGURED"
    coverage: float | None = None
    bugs: int = 0
    vulnerabilities: int = 0
    code_smells: int = 0
    url: str = ""
    updated_at: str = ""
    checks: list[QualityCheck] = Field(default_factory=list)
    evidence_source: Literal["normalized-report", "github-actions", "none"] = "none"


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


class DailyCost(BaseModel):
    date: str  # ISO YYYY-MM-DD, usage date in the billing account timezone
    cost: float = 0.0
    credits: float = 0.0
    net_cost: float = 0.0


class DailyCostSeries(BaseModel):
    currency: str = "USD"
    period: CostPeriod
    days: list[DailyCost] = Field(default_factory=list)
    previous_period: CostPeriod
    previous_total_net_cost: float = 0.0


# ── Service Factory ──────────────────────────────────────────────────


class ServiceFactoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
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
    quality_profile: Literal["python", "node", "static"] | None = None
    quality_working_directory: str = "."
    coverage_threshold: float = Field(default=70.0, ge=0, le=100)
    sonar_project_key: str = ""  # Deprecated compatibility input.
    sonar_organization: str = ""  # Deprecated compatibility input.
    validation_targets: list[str] = Field(default_factory=list)


class ServiceFactoryTemplate(BaseModel):
    name: str
    description: str
    type: str  # api, web, worker, integration


class ServiceFactoryPlan(BaseModel):
    repository: str
    service_name: str
    generated_files: list[str] = Field(default_factory=list)
    checklist: list[str] = Field(default_factory=list)
    yaml_contract: str = ""
    caller_pr_check: str = ""
    caller_release_candidate: str = ""
    caller_promote: str = ""
    caller_rollback: str = ""
    platform_deploy_workflow: str = ""
    platform_rollback_workflow: str = ""
    semantic_release_workflow: str = ""
    catalog_entry: str = ""
    agent_prompt: str = ""
    labels_manifest: str = ""
    quality_config: str = ""
    sonar_properties: str = ""  # Deprecated compatibility output; always empty.


# ── Health ────────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.5.0"
    mock_mode: bool = True


class ServiceHealthItem(BaseModel):
    service_name: str
    project_id: str
    region: str
    status: str = "unknown"
    checked_at: str = ""
    error: str = ""


class ServicesHealthResponse(BaseModel):
    status: str = "ok"
    services: list[ServiceHealthItem] = Field(default_factory=list)
