"""Pydantic models for the Engineering Platform API."""

from __future__ import annotations

from typing import Any, Literal
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)


# ── Catalog ──────────────────────────────────────────────────────────


class ValidationTarget(BaseModel):
    name: str
    type: str = "external_source"
    description: str = ""


class ServiceQualityConfig(BaseModel):
    enabled: bool = False
    profile: Literal["python", "node", "go", "static"] | None = None
    coverage_threshold: float = 70.0
    policy_version: str = "oss-v2"
    differential_threshold: float = 80.0
    evidence_services: list[str] = Field(default_factory=list)


class ServiceDeploymentConfig(BaseModel):
    executor: Literal["auto", "cloud_build"] = "auto"
    enabled: bool = True
    workflow_file: str = "platform-deploy.yml"
    image_name: str = ""
    artifact_repository: str = "cgm-sanplat-repo"
    build_context: str = "."
    dockerfile_path: str = "Dockerfile"
    runtime_kind: Literal["cloud_run_service", "cloud_run_job"] = "cloud_run_service"
    private_runtime: bool = False
    health_path: str = "/"
    api_base_url: str = ""
    api_candidate_base_url: str = ""


class FinOpsLabels(BaseModel):
    service: str = ""
    env: str = ""
    owner: str = ""
    cost_center: str = ""


class OperationalSecret(BaseModel):
    """Trusted metadata only. Values never belong in the catalog."""

    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    secret_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,255}$")
    description: str = ""
    required: bool = True
    editable: bool = False


class InfrastructureResource(BaseModel):
    """Auxiliary GCP identity metadata; never deployment or log authority."""

    model_config = ConfigDict(extra="forbid", strict=True)
    resource_type: Literal[
        "firestore_database",
        "gcs_bucket",
        "cloud_tasks_queue",
        "cloud_scheduler_job",
        "artifact_registry_repository",
        "secret_manager_secret",
        "billing_budget",
    ]
    resource_name: str = Field(min_length=1, max_length=512, pattern=r"^\S+$")
    description: str = Field(default="", max_length=1024)


class ServiceLogsCapability(BaseModel):
    """Public capability only; reader identities never leave the authority."""

    enabled: bool = False
    configured: bool = False


class InventorySource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["cloud_run_inventory"]
    observed_at: str
    project: str
    region: str


class CatalogService(BaseModel):
    service_name: str
    management_mode: Literal["managed", "observability_only"] = "managed"
    repository: str | None
    owner: str | None
    inventory_source: InventorySource | None = None
    cost_center: str = ""
    project_id: str
    region: str
    environment: str = "prod"
    release_model: str = "managed-release"
    release_policy: str = "oss-v2"
    validation_targets: list[ValidationTarget] = Field(default_factory=list)
    quality: ServiceQualityConfig = Field(default_factory=ServiceQualityConfig)
    deployment: ServiceDeploymentConfig = Field(default_factory=ServiceDeploymentConfig)
    finops: FinOpsLabels = Field(default_factory=FinOpsLabels)
    logs: ServiceLogsCapability = Field(default_factory=ServiceLogsCapability)
    deployment_ready: bool = False
    deployment_blockers: list[str] = Field(default_factory=list)
    operational_secrets: list[OperationalSecret] = Field(default_factory=list)
    infrastructure_resources: list[InfrastructureResource] = Field(
        default_factory=list, max_length=256
    )

    @model_serializer(mode="wrap")
    def serialize_optional_infrastructure(self, handler: SerializerFunctionWrapHandler):
        # No serializer return annotation: Pydantic must preserve the structured
        # CatalogService/ServiceDetail OpenAPI schema rather than a generic dict.
        data: dict[str, Any] = handler(self)
        # Older services keep their exact response shape. References are emitted
        # only when present and never become independent managed catalog entries.
        if not self.infrastructure_resources:
            data.pop("infrastructure_resources", None)
        return data

    @model_validator(mode="after")
    def validate_management_mode(self):
        if self.management_mode == "observability_only":
            if (
                self.repository is not None
                or self.owner is not None
                or self.inventory_source is None
                or self.deployment.enabled
                or self.operational_secrets
                or self.infrastructure_resources
                or self.quality.enabled
            ):
                raise ValueError(
                    "Observation-only resources cannot carry managed configuration"
                )
        elif self.repository is None or self.owner is None:
            raise ValueError("Managed resources require repository and owner")
        return self


class ServiceTraffic(BaseModel):
    revision: str = ""
    percent: int = 0
    tag: str = ""


class ServiceDetail(CatalogService):
    status: str = "unknown"
    url: str = ""
    latest_ready_revision: str = ""
    serving_revision: str = ""
    runtime_sha: str = ""
    last_job_execution: str = ""
    last_job_execution_status: str = ""
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
    release_id: str = ""
    release_group_id: str = ""
    source_sha: str = ""
    artifact_digest: str = ""
    revision: str = ""
    action: ReleaseServiceAction = "deployed"
    github_run_url: str = ""
    created_at: str = ""


class ReleaseCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    version: str
    status: str = "candidate"  # "candidate" | "promoted" | "rolled_back"
    release_id: str = ""
    release_group_id: str = ""
    source_sha: str = ""
    artifact_digest: str = ""
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
    "UNKNOWN",
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


class ReleaseAuthorizationConsumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=8192)
    repository: str = Field(min_length=1, max_length=256)
    service_name: str = Field(min_length=1, max_length=128)
    tag: str = Field(min_length=1, max_length=128)
    sha: str = Field(min_length=40, max_length=64)
    github_deployment_id: str = Field(min_length=1, max_length=64)
    kind: Literal["deploy", "rollback"]
    target_revision: str = Field(default="", max_length=128)
    configuration_hash: str = Field(default="", max_length=64)


class ReleaseAuthorizationConsumeResponse(BaseModel):
    accepted: bool = True
    jti: str


class DeploymentItem(BaseModel):
    origin: Literal["managed", "external_verified"] = "managed"
    reason: str = ""
    image_digest: str = ""
    evidence_url: str = ""
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


class DeploymentExecutionEvent(BaseModel):
    """Private, monotonic event emitted by the central release engine."""

    model_config = ConfigDict(extra="forbid")

    build_id: str = Field(min_length=1, max_length=128)
    fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(ge=1)
    stage: str = Field(min_length=1, max_length=64)
    status: DeploymentStageStatus
    candidate_revision: str = ""
    candidate_url: str = ""
    production_revision: str = ""
    production_url: str = ""
    image_digest: str = ""
    error: str = ""


class DeploymentList(BaseModel):
    items: list[DeploymentItem] = Field(default_factory=list)
    total: int = 0


class DeploymentOverviewItem(BaseModel):
    service_name: str
    status: str = "unknown"
    latest_ready_revision: str = ""
    serving_revision: str = ""
    runtime_sha: str = ""
    last_job_execution: str = ""
    last_job_execution_status: str = ""
    deployment_ready: bool = False
    deployment_blockers: list[str] = Field(default_factory=list)
    last_deployment: DeploymentItem | None = None


class DeploymentOverview(BaseModel):
    items: list[DeploymentOverviewItem] = Field(default_factory=list)
    generated_at: str = ""


class AuthSession(BaseModel):
    authenticated: bool = False
    can_deploy: bool = False
    can_view_logs: bool = False
    can_view_catalog: bool = False
    can_query_databases: bool = False
    database_session_required: bool = False
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
    profile: Literal["python", "node", "go", "static"]
    workflow_run_url: str = ""
    generated_at: str
    coverage: float | None = Field(default=None, ge=0, le=100)
    coverage_threshold: float | None = Field(default=None, ge=0, le=100)
    policy_version: str = "oss-v1"
    base_sha: str = ""
    differential_coverage: float | None = Field(default=None, ge=0, le=100)
    differential_threshold: float | None = Field(default=None, ge=0, le=100)
    changed_lines: int | None = Field(default=None, ge=0)
    covered_changed_lines: int | None = Field(default=None, ge=0)
    tool_versions: dict[str, str] = Field(default_factory=dict)
    checks: list[QualityCheck] = Field(min_length=1)


class QualityReport(QualityReportCreate):
    quality_gate_status: QualityGateStatus
    received_at: str


ReleaseExecutionStatus = Literal[
    "received",
    "waiting_github",
    "submission_pending",
    "submitting",
    "running_quality",
    "quality_passed",
    "quality_failed",
    "release_planned",
    "no_release",
    "publish_pending",
    "released",
    "failed",
    "unknown",
]


class ReleasePlan(BaseModel):
    """Write-free output of the pinned semantic release planner."""

    model_config = ConfigDict(extra="forbid")

    next_version: str = Field(default="", max_length=64)
    git_tag: str = Field(default="", max_length=128)
    release_type: Literal["patch", "minor", "major", "none"] = "none"
    notes: str = Field(default="", max_length=100_000)
    config_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


class ReleaseExecutionEvent(BaseModel):
    """Monotonic event from a trusted Actions or Cloud Build quality engine."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str = Field(min_length=1, max_length=128)
    provider_run_id: str = Field(min_length=1, max_length=128)
    fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    sequence: int = Field(ge=1)
    status: Literal[
        "running_quality",
        "quality_passed",
        "quality_failed",
        "release_planned",
        "no_release",
        "failed",
    ]
    report: QualityReportCreate | None = None
    report_hash: str = Field(default="", max_length=64, pattern=r"^$|^[0-9a-f]{64}$")
    release_plan: ReleasePlan | None = None
    error: str = Field(default="", max_length=2_000)


class QualityProject(BaseModel):
    policy_version: str = "oss-v1"
    base_sha: str = ""
    differential_coverage: float | None = Field(default=None, ge=0, le=100)
    differential_threshold: float | None = Field(default=None, ge=0, le=100)
    changed_lines: int | None = Field(default=None, ge=0)
    covered_changed_lines: int | None = Field(default=None, ge=0)

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


class CostComponentCoverage(BaseModel):
    component_id: str = ""
    attributed: bool = False
    first_usage_at: str | None = None
    latest_usage_at: str | None = None
    observed_hours: int = 0


class CostItem(BaseModel):
    project_id: str = ""
    service_name: str = ""
    gcp_service: str = ""
    cost: float = 0.0
    credits: float = 0.0
    net_cost: float = 0.0
    currency: str = "USD"
    attributed: bool = True
    observed_hours: int = 0
    components: list[CostComponentCoverage] = Field(default_factory=list)
    first_usage_at: str | None = None
    latest_usage_at: str | None = None


class CostPeriod(BaseModel):
    start: str
    end: str


class BillingQuality(BaseModel):
    status: Literal["partial", "no_data", "unavailable", "mixed_currency"] = "no_data"
    basis: str = "exported_usage"
    timezone: str = "America/Bogota"
    is_complete: bool = False
    retrieved_at: str | None = None
    latest_export_at: str | None = None
    first_usage_at: str | None = None
    latest_usage_at: str | None = None
    rows: int = 0
    reason: str = ""


class CostSummary(BaseModel):
    currency: str = "USD"
    period: CostPeriod
    total_cost: float | None = None
    total_credits: float | None = None
    total_net_cost: float | None = None
    items: list[CostItem] = Field(default_factory=list)
    data_quality: BillingQuality = Field(default_factory=BillingQuality)
    cloud_build: "CloudBuildUsage | None" = None
    estimates_included_in_total: bool = False


class CloudBuildUsage(BaseModel):
    """Server-owned Cloud Build accounting, independent of the billing export."""

    month: str = ""
    release_minutes: float = 0.0
    deployment_minutes: float = 0.0
    total_minutes: float = 0.0
    project_id: str = ""
    other_minutes: float = 0.0
    project_minutes: float = 0.0
    estimated_cost_usd: float = 0.0
    project_estimated_cost_usd: float = 0.0
    billed_cost_usd: float | None = None
    billed_credits_usd: float | None = None
    billed_net_cost_usd: float | None = None
    billing_exported_at: str | None = None
    billing_updated_at: str | None = None
    updated_at: str | None = None
    data_status: str = "partial"
    billing_data_status: str = "unavailable"
    build_count: int = 0
    pending_build_count: int = 0
    policy_violation_count: int = 0
    unpriced_build_count: int = 0
    alert_scope: str = "project"
    minute_price_usd: float = 0.0
    alert_thresholds: list[int] = Field(default_factory=list)
    alert_thresholds_reached: list[int] = Field(default_factory=list)


class DailyCost(BaseModel):
    date: str  # ISO YYYY-MM-DD, consumption date in America/Bogota
    cost: float | None = None
    credits: float | None = None
    net_cost: float | None = None
    has_data: bool = False


class DailyCostSeries(BaseModel):
    currency: str = "USD"
    period: CostPeriod
    days: list[DailyCost] = Field(default_factory=list)
    previous_period: CostPeriod
    previous_total_net_cost: float | None = None
    previous_comparable: bool = False
    previous_comparison_reason: str = "incomplete_or_unequal_daily_coverage"
    data_quality: BillingQuality = Field(default_factory=BillingQuality)


class CostChange(BaseModel):
    project_id: str = ""
    gcp_service: str = ""
    service_name: str = ""
    currency: str = "USD"
    current: CostItem | None = None
    previous: CostItem | None = None
    comparable: bool = False
    reason: str = "missing_data"
    net_change: float | None = None
    percent_change: float | None = None


class CostComparison(BaseModel):
    timezone: str = "America/Bogota"
    current: CostSummary
    previous: CostSummary
    current_start_at: str
    current_end_at: str
    previous_start_at: str
    previous_end_at: str
    items: list[CostChange] = Field(default_factory=list)
    comparable: bool = False
    net_change: float | None = None
    reason: str = "missing_data"
    is_final: bool = False


# ── Service Factory ──────────────────────────────────────────────────


class ServiceFactoryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=256
    )
    service_name: str = Field(pattern=r"^[a-z](?:[a-z0-9-]*[a-z0-9])?$", max_length=63)
    service_type: Literal["api", "web", "worker", "integration"]
    runtime: Literal["python", "node", "static"]
    runtime_kind: Literal["cloud_run_service", "cloud_run_job"] = "cloud_run_service"
    gcp_project: str = Field(pattern=r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
    region: str = Field(
        default="us-central1", pattern=r"^[a-z]+(?:-[a-z]+)+[0-9]+$", max_length=63
    )
    owner: str = Field(pattern=r"^[a-z0-9_-]+$", max_length=63)
    cost_center: str = Field(default="", pattern=r"^[a-z0-9_-]*$", max_length=63)
    environment: Literal["prod", "staging", "dev"] = "prod"
    cloud_run_service_name: str = Field(
        default="",
        pattern=r"^(?:[a-z0-9][a-z0-9._-]*)?$",
        max_length=128,
        description="Deprecated image-name override only; service_name is the runtime resource ID.",
    )
    health_path: str = Field(default="/health", pattern=r"^/[^\s]*$", max_length=512)
    openapi_path: str = Field(
        default="/openapi.json", pattern=r"^/[^\s]*$", max_length=512
    )
    quality_profile: Literal["python", "node", "static"] | None = None
    quality_working_directory: str = Field(
        default=".",
        pattern=r"^(?:\.|[A-Za-z0-9_-]+(?:[./][A-Za-z0-9_-]+)*)$",
        max_length=256,
    )
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
    quality_sources: str = ""
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


# ── Read-only, sanitized runtime logs ──────────────────────────────────

LogSeverity = Literal[
    "DEFAULT",
    "DEBUG",
    "INFO",
    "NOTICE",
    "WARNING",
    "ERROR",
    "CRITICAL",
    "ALERT",
    "EMERGENCY",
]
LogDeferralReason = Literal["cadence", "queue", "budget", "pending", "overload"]


class LogEntry(BaseModel):
    id: str
    timestamp: str
    severity: LogSeverity = "DEFAULT"
    message: str
    payload: object = None
    revision: str | None = None
    execution: str | None = None
    task_index: int | None = None
    trace: str | None = None
    span_id: str | None = None


class ServiceLogsResponse(BaseModel):
    resource_generation: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    entries: list[LogEntry] = Field(default_factory=list)
    status: Literal["fresh", "stale", "throttled", "unavailable", "disabled"]
    truncated: bool = False
    cache_evicted: bool = False
    queue_position: int | None = Field(default=None, ge=1, le=4096)
    queue_wait_seconds: int | None = Field(default=None, ge=0)
    overloaded: bool = False
    deferral_reason: LogDeferralReason | None = None
    next_poll_seconds: int = 5
    explorer_url: str
    observed_at: str | None = None
    last_success_at: str | None = None
    window_start: str
    cache_age_seconds: float | None = None
    limitations: list[str] = Field(default_factory=list)


class ServiceLogsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=200, ge=1, le=200)
    lookback_minutes: int = Field(default=15, ge=1, le=15)
    severity: LogSeverity = "DEFAULT"
    text: str = Field(default="", max_length=200)
    revision: str | None = Field(
        default=None, max_length=128, pattern=r"^[a-z][a-z0-9-]{0,127}$"
    )
    execution: str | None = Field(
        default=None, max_length=128, pattern=r"^[a-z][a-z0-9-]{0,127}$"
    )
    task_index: int | None = Field(default=None, ge=0, le=999999)
