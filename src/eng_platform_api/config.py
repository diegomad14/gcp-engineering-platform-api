"""Configuration for the Engineering Platform API.

All integrations default to mock mode. Real GCP/GitHub/SonarQube
integrations require explicit environment variable configuration.
"""

import json
import os
import re
from dataclasses import dataclass, field

_RELEASE_PLANNER_BROKEN_DIGEST = (
    "us-central1-docker.pkg.dev/cgm-assistant-prod/cgm-sanplat-repo/"
    "release-planner@sha256:2807a7bc318215ed9473aaf456d614e96d276bc449a5aad5c103974db5f3f392"
)
_RELEASE_PLANNER_RECOVERED_DIGEST = (
    "us-central1-docker.pkg.dev/cgm-assistant-prod/cgm-sanplat-repo/"
    "release-planner@sha256:6db0fc9bc323f9260d7d529ee9142777db848b31c750fb5015c34fef8a866591"
)


def _effective_release_planner_image(configured: str) -> str:
    """Roll the single known-broken planner digest forward without mutating Cloud Run."""
    image = configured.strip()
    if image == _RELEASE_PLANNER_BROKEN_DIGEST:
        return _RELEASE_PLANNER_RECOVERED_DIGEST
    return image


def _service_account_subject(value: str) -> str:
    """Normalize a Cloud Build resource name to the OIDC email subject."""
    marker = "/serviceAccounts/"
    return value.rsplit(marker, 1)[-1] if marker in value else value


@dataclass
class BillingConfig:
    enabled: bool = False
    bigquery_project_id: str = ""
    bigquery_dataset: str = "billing_export"
    bigquery_table: str = "gcp_billing_export_resource_v1_XXXXXX"
    bigquery_location: str = "US"
    env_label: str = "env"
    owner_label: str = "owner"
    cost_center_label: str = "cost_center"


@dataclass
class MonitoringConfig:
    enabled: bool = False
    gcp_project_id: str = ""


@dataclass
class GitHubConfig:
    enabled: bool = False
    token: str = ""
    app_id: str = ""
    installation_id: str = ""
    private_key: str = ""
    deployment_workflow: str = "platform-deploy.yml"
    rollback_workflow: str = "platform-rollback.yml"
    platform_api_url: str = ""
    release_signing_private_key: str = ""
    release_signing_public_key: str = ""
    release_authorization_collection: str = "release_authorizations"
    billing_owner: str = ""
    included_private_minutes: int = 2000


@dataclass
class CloudBuildConfig:
    """Configuration for the managed, economy deployment executor.

    The feature stays off until the GitHub connection, its repositories and IAM
    are installed.  The executor image is deliberately a digest, never a tag:
    a release must not silently acquire new orchestration code.
    """

    enabled: bool = False
    mode: str = "auto"
    project_id: str = ""
    region: str = "us-central1"
    service_account: str = ""
    executor_image: str = ""
    execution_collection: str = "deployment_executions"
    evidence_bucket: str = ""
    repositories: dict[str, str] = field(default_factory=dict)
    callback_service_account: str = ""
    enabled_services: tuple[str, ...] = ()
    cloud_build_only_services: tuple[str, ...] = ()
    deploy_dispatch_timeout_seconds: int = 90


@dataclass
class ReleaseOrchestratorConfig:
    """Private CI/release fallback configuration.

    This plane is intentionally separate from the deployment executor: quality
    images never receive deployment permissions and the deployment image does
    not grow test or scanner tooling.
    """

    enabled: bool = False
    execution_collection: str = "release_executions"
    circuit_collection: str = "executor_circuits"
    webhook_delivery_collection: str = "github_webhook_deliveries"
    webhook_secret: str = ""
    enabled_services: tuple[str, ...] = ()
    canary_services: tuple[str, ...] = ()
    service_account: str = ""
    callback_service_account: str = ""
    reconciler_service_account: str = ""
    quality_node_image: str = ""
    quality_python_image: str = ""
    release_planner_image: str = ""
    postgres_image: str = ""
    github_mode_variable: str = "ENG_PLATFORM_CI_EXECUTOR"
    github_health_repository: str = ""
    github_health_workflow: str = "eng-platform-actions-health.yml"
    build_minute_price_usd: float = 0.006
    usage_alert_minutes: tuple[int, ...] = (2000, 2250, 2500)
    release_dispatch_timeout_seconds: int = 600
    artemis_web_sha: str = ""


@dataclass
class AuthConfig:
    github_client_id: str = ""
    github_client_secret: str = ""
    session_secret: str = ""
    frontend_url: str = "http://localhost:5173"
    allowed_logins: tuple[str, ...] = ()
    trust_iap_identity: bool = False


@dataclass
class MCPConfig:
    """Configuration for the remote Model Context Protocol surface."""

    enabled: bool = False
    public_base_url: str = ""
    issuer_url: str = ""
    audit_collection: str = "eng_platform_mcp_audit"
    oauth_collection: str = "eng_platform_mcp_oauth"
    access_token_ttl_seconds: int = 3600
    refresh_token_ttl_seconds: int = 2_592_000
    mutation_limit_per_hour: int = 10


@dataclass
class SonarQubeConfig:
    enabled: bool = False
    token: str = ""
    host_url: str = "https://sonarcloud.io"


@dataclass
class QualityConfig:
    ingest_token: str = ""
    bucket: str = ""
    prefix: str = "quality"
    local_store_path: str = "data"
    stale_after_hours: int = 168


@dataclass
class PlatformConfig:
    mock_mode: bool = True
    billing: BillingConfig = field(default_factory=BillingConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    cloud_build: CloudBuildConfig = field(default_factory=CloudBuildConfig)
    release_orchestrator: ReleaseOrchestratorConfig = field(
        default_factory=ReleaseOrchestratorConfig
    )
    auth: AuthConfig = field(default_factory=AuthConfig)
    mcp: MCPConfig = field(default_factory=MCPConfig)
    sonarqube: SonarQubeConfig = field(default_factory=SonarQubeConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)


def load_config() -> PlatformConfig:
    """Load configuration from environment variables."""

    mock_mode = os.getenv("ENG_PLATFORM_MOCK_MODE", "false").lower() == "true"

    billing = BillingConfig(
        enabled=os.getenv("ENG_PLATFORM_BILLING_ENABLED", "false").lower() == "true",
        bigquery_project_id=os.getenv("ENG_PLATFORM_BQ_PROJECT_ID", ""),
        bigquery_dataset=os.getenv("ENG_PLATFORM_BQ_DATASET", "billing_export"),
        bigquery_table=os.getenv(
            "ENG_PLATFORM_BQ_TABLE", "gcp_billing_export_resource_v1_XXXXXX"
        ),
        bigquery_location=os.getenv("ENG_PLATFORM_BQ_LOCATION", "US"),
        env_label=os.getenv("ENG_PLATFORM_LABEL_ENV", "env"),
        owner_label=os.getenv("ENG_PLATFORM_LABEL_OWNER", "owner"),
        cost_center_label=os.getenv("ENG_PLATFORM_LABEL_COST_CENTER", "cost_center"),
    )

    monitoring = MonitoringConfig(
        enabled=os.getenv("ENG_PLATFORM_MONITORING_ENABLED", "false").lower() == "true",
        gcp_project_id=os.getenv("ENG_PLATFORM_GCP_PROJECT_ID", ""),
    )

    github = GitHubConfig(
        enabled=os.getenv("ENG_PLATFORM_GITHUB_ENABLED", "false").lower() == "true",
        token=os.getenv("ENG_PLATFORM_GITHUB_TOKEN", "").strip(),
        app_id=os.getenv("ENG_PLATFORM_GITHUB_APP_ID", ""),
        installation_id=os.getenv("ENG_PLATFORM_GITHUB_INSTALLATION_ID", ""),
        private_key=os.getenv("ENG_PLATFORM_GITHUB_PRIVATE_KEY", "").replace(
            "\\n", "\n"
        ),
        deployment_workflow=os.getenv(
            "ENG_PLATFORM_GITHUB_DEPLOYMENT_WORKFLOW", "platform-deploy.yml"
        ),
        rollback_workflow=os.getenv(
            "ENG_PLATFORM_GITHUB_ROLLBACK_WORKFLOW", "platform-rollback.yml"
        ),
        platform_api_url=os.getenv("ENG_PLATFORM_API_URL", "").rstrip("/"),
        release_signing_private_key=os.getenv(
            "ENG_PLATFORM_RELEASE_SIGNING_PRIVATE_KEY", ""
        ).replace("\\n", "\n"),
        release_signing_public_key=os.getenv(
            "ENG_PLATFORM_RELEASE_SIGNING_PUBLIC_KEY", ""
        ).replace("\\n", "\n"),
        release_authorization_collection=os.getenv(
            "ENG_PLATFORM_RELEASE_AUTH_FIRESTORE_COLLECTION",
            "release_authorizations",
        ),
        billing_owner=os.getenv("ENG_PLATFORM_GITHUB_BILLING_OWNER", "").strip(),
        included_private_minutes=int(
            os.getenv("ENG_PLATFORM_GITHUB_INCLUDED_PRIVATE_MINUTES", "2000")
        ),
    )

    repositories_raw = os.getenv("ENG_PLATFORM_CLOUD_BUILD_REPOSITORIES_JSON", "{}")
    try:
        repositories = json.loads(repositories_raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "ENG_PLATFORM_CLOUD_BUILD_REPOSITORIES_JSON must be JSON"
        ) from exc
    if not isinstance(repositories, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in repositories.items()
    ):
        raise ValueError("ENG_PLATFORM_CLOUD_BUILD_REPOSITORIES_JSON must map strings")

    release_executor_image = os.getenv(
        "ENG_PLATFORM_RELEASE_EXECUTOR_IMAGE", ""
    ).strip()
    cloud_build_executor_image = os.getenv(
        "ENG_PLATFORM_CLOUD_BUILD_EXECUTOR_IMAGE", ""
    ).strip()
    if (
        release_executor_image
        and cloud_build_executor_image
        and release_executor_image != cloud_build_executor_image
    ):
        raise ValueError("Release executor image settings must match")

    cloud_build = CloudBuildConfig(
        enabled=os.getenv("ENG_PLATFORM_CLOUD_BUILD_ENABLED", "false").lower()
        == "true",
        mode=os.getenv("ENG_PLATFORM_DEPLOY_EXECUTOR_MODE", "auto").strip(),
        project_id=os.getenv("ENG_PLATFORM_CLOUD_BUILD_PROJECT_ID", "").strip(),
        region=os.getenv("ENG_PLATFORM_CLOUD_BUILD_REGION", "us-central1").strip(),
        service_account=os.getenv(
            "ENG_PLATFORM_CLOUD_BUILD_SERVICE_ACCOUNT", ""
        ).strip(),
        executor_image=release_executor_image or cloud_build_executor_image,
        execution_collection=os.getenv(
            "ENG_PLATFORM_DEPLOYMENT_EXECUTION_FIRESTORE_COLLECTION",
            "deployment_executions",
        ).strip(),
        evidence_bucket=os.getenv(
            "ENG_PLATFORM_CLOUD_BUILD_EVIDENCE_BUCKET", ""
        ).strip(),
        repositories=repositories,
        callback_service_account=os.getenv(
            "ENG_PLATFORM_CLOUD_BUILD_CALLBACK_SERVICE_ACCOUNT", ""
        ).strip(),
        enabled_services=tuple(
            service.strip()
            for service in os.getenv(
                "ENG_PLATFORM_CLOUD_BUILD_ENABLED_SERVICES", ""
            ).split(",")
            if service.strip()
        ),
        cloud_build_only_services=tuple(
            service.strip()
            for service in os.getenv(
                "ENG_PLATFORM_CLOUD_BUILD_ONLY_SERVICES", ""
            ).split(",")
            if service.strip()
        ),
        deploy_dispatch_timeout_seconds=int(
            os.getenv("ENG_PLATFORM_DEPLOY_DISPATCH_TIMEOUT_SECONDS", "90")
        ),
    )
    if not set(cloud_build.cloud_build_only_services).issubset(
        cloud_build.enabled_services
    ):
        raise ValueError("Cloud Build-only services must be enabled services")
    if cloud_build.deploy_dispatch_timeout_seconds <= 0:
        raise ValueError("Deploy dispatch timeout must be positive")
    if cloud_build.enabled:
        if not (
            cloud_build.project_id
            and cloud_build.service_account
            and cloud_build.callback_service_account
            and cloud_build.evidence_bucket
        ):
            raise ValueError(
                "Cloud Build project, build/callback identity and evidence bucket are required"
            )
        if "@sha256:" not in cloud_build.executor_image:
            raise ValueError(
                "Cloud Build release executor image must be pinned by digest"
            )
        if _service_account_subject(cloud_build.service_account) != (
            _service_account_subject(cloud_build.callback_service_account)
        ):
            raise ValueError(
                "Cloud Build callback and build service accounts must identify the same account"
            )

    release_orchestrator = ReleaseOrchestratorConfig(
        enabled=os.getenv("ENG_PLATFORM_RELEASE_ORCHESTRATOR_ENABLED", "false").lower()
        == "true",
        execution_collection=os.getenv(
            "ENG_PLATFORM_RELEASE_EXECUTION_FIRESTORE_COLLECTION",
            "release_executions",
        ).strip(),
        circuit_collection=os.getenv(
            "ENG_PLATFORM_EXECUTOR_CIRCUIT_FIRESTORE_COLLECTION",
            "executor_circuits",
        ).strip(),
        webhook_delivery_collection=os.getenv(
            "ENG_PLATFORM_GITHUB_WEBHOOK_DELIVERY_FIRESTORE_COLLECTION",
            "github_webhook_deliveries",
        ).strip(),
        webhook_secret=os.getenv("ENG_PLATFORM_GITHUB_WEBHOOK_SECRET", ""),
        enabled_services=tuple(
            service.strip()
            for service in os.getenv("ENG_PLATFORM_RELEASE_ENABLED_SERVICES", "").split(
                ","
            )
            if service.strip()
        ),
        canary_services=tuple(
            service.strip()
            for service in os.getenv("ENG_PLATFORM_RELEASE_CANARY_SERVICES", "").split(
                ","
            )
            if service.strip()
        ),
        service_account=os.getenv(
            "ENG_PLATFORM_RELEASE_QUALITY_SERVICE_ACCOUNT",
            os.getenv("ENG_PLATFORM_CLOUD_BUILD_SERVICE_ACCOUNT", ""),
        ).strip(),
        callback_service_account=os.getenv(
            "ENG_PLATFORM_RELEASE_CALLBACK_SERVICE_ACCOUNT",
            os.getenv("ENG_PLATFORM_CLOUD_BUILD_CALLBACK_SERVICE_ACCOUNT", ""),
        ).strip(),
        reconciler_service_account=os.getenv(
            "ENG_PLATFORM_RELEASE_RECONCILER_SERVICE_ACCOUNT", ""
        ).strip(),
        quality_node_image=os.getenv("ENG_PLATFORM_QUALITY_NODE_IMAGE", "").strip(),
        quality_python_image=os.getenv("ENG_PLATFORM_QUALITY_PYTHON_IMAGE", "").strip(),
        release_planner_image=_effective_release_planner_image(
            os.getenv("ENG_PLATFORM_RELEASE_PLANNER_IMAGE", "")
        ),
        postgres_image=os.getenv("ENG_PLATFORM_RELEASE_POSTGRES_IMAGE", "").strip(),
        github_mode_variable=os.getenv(
            "ENG_PLATFORM_GITHUB_MODE_VARIABLE", "ENG_PLATFORM_CI_EXECUTOR"
        ).strip(),
        github_health_repository=os.getenv(
            "ENG_PLATFORM_GITHUB_HEALTH_REPOSITORY", ""
        ).strip(),
        github_health_workflow=os.getenv(
            "ENG_PLATFORM_GITHUB_HEALTH_WORKFLOW",
            "eng-platform-actions-health.yml",
        ).strip(),
        build_minute_price_usd=float(
            os.getenv("ENG_PLATFORM_CLOUD_BUILD_MINUTE_PRICE_USD", "0.006")
        ),
        usage_alert_minutes=tuple(
            int(value.strip())
            for value in os.getenv(
                "ENG_PLATFORM_CLOUD_BUILD_USAGE_ALERT_MINUTES", "2000,2250,2500"
            ).split(",")
            if value.strip()
        ),
        release_dispatch_timeout_seconds=int(
            os.getenv("ENG_PLATFORM_RELEASE_DISPATCH_TIMEOUT_SECONDS", "600")
        ),
        artemis_web_sha=os.getenv("ENG_PLATFORM_ARTEMIS_WEB_SHA", "").strip().lower(),
    )
    if release_orchestrator.enabled:
        if not release_orchestrator.webhook_secret:
            raise ValueError(
                "ENG_PLATFORM_GITHUB_WEBHOOK_SECRET is required for release orchestration"
            )
        if not release_orchestrator.service_account:
            raise ValueError("ENG_PLATFORM_RELEASE_QUALITY_SERVICE_ACCOUNT is required")
        if not release_orchestrator.callback_service_account:
            raise ValueError(
                "ENG_PLATFORM_RELEASE_CALLBACK_SERVICE_ACCOUNT is required"
            )
        if not release_orchestrator.reconciler_service_account:
            raise ValueError(
                "ENG_PLATFORM_RELEASE_RECONCILER_SERVICE_ACCOUNT is required"
            )
        if _service_account_subject(
            release_orchestrator.callback_service_account
        ) != _service_account_subject(release_orchestrator.service_account):
            raise ValueError(
                "Release callback and quality build service accounts must match"
            )
        if _service_account_subject(
            release_orchestrator.reconciler_service_account
        ) in {
            _service_account_subject(release_orchestrator.service_account),
            _service_account_subject(release_orchestrator.callback_service_account),
        }:
            raise ValueError(
                "Release reconciler service account must be separate from the build identity"
            )
        if _service_account_subject(
            release_orchestrator.service_account
        ) == _service_account_subject(cloud_build.service_account):
            raise ValueError(
                "Release quality and deployment builds require separate service accounts"
            )
        if not (github.app_id and github.installation_id and github.private_key):
            raise ValueError(
                "GitHub App credentials are required for release orchestration"
            )
        if not github.platform_api_url:
            raise ValueError(
                "ENG_PLATFORM_API_URL is required for release orchestration"
            )
        if not cloud_build.enabled or not cloud_build.project_id:
            raise ValueError("Cloud Build must be configured for release fallback")
        if not cloud_build.evidence_bucket:
            raise ValueError("Cloud Build evidence bucket is required")
        quality_bucket = os.getenv("ENG_PLATFORM_QUALITY_BUCKET", "").strip()
        if not quality_bucket:
            raise ValueError("Release orchestration requires a quality evidence bucket")
        missing_repositories = (
            set(release_orchestrator.enabled_services)
            | set(release_orchestrator.canary_services)
        ).difference(cloud_build.repositories)
        if missing_repositories:
            raise ValueError(
                "Connected repositories are missing for release services: "
                + ", ".join(sorted(missing_repositories))
            )
        images = (
            release_orchestrator.quality_node_image,
            release_orchestrator.quality_python_image,
            release_orchestrator.release_planner_image,
        )
        if any("@sha256:" not in image for image in images):
            raise ValueError("Release orchestrator images must be pinned by digest")
        overlap = set(release_orchestrator.enabled_services).intersection(
            release_orchestrator.canary_services
        )
        if overlap:
            raise ValueError("Release services cannot be both canary and auto")
        if release_orchestrator.build_minute_price_usd < 0:
            raise ValueError("Cloud Build minute price cannot be negative")
        if any(value <= 0 for value in release_orchestrator.usage_alert_minutes):
            raise ValueError("Cloud Build usage alert thresholds must be positive")
        if release_orchestrator.release_dispatch_timeout_seconds <= 0:
            raise ValueError("Release dispatch timeout must be positive")
        paired_web_sha = release_orchestrator.artemis_web_sha
        if paired_web_sha and not re.fullmatch(r"[0-9a-f]{40}", paired_web_sha):
            raise ValueError("The paired Artemis Web SHA must be a full commit SHA")

    auth = AuthConfig(
        github_client_id=os.getenv("ENG_PLATFORM_GITHUB_OAUTH_CLIENT_ID", ""),
        github_client_secret=os.getenv("ENG_PLATFORM_GITHUB_OAUTH_CLIENT_SECRET", ""),
        session_secret=os.getenv("ENG_PLATFORM_SESSION_SECRET", ""),
        frontend_url=os.getenv(
            "ENG_PLATFORM_FRONTEND_URL", "http://localhost:5173"
        ).rstrip("/"),
        allowed_logins=tuple(
            login.strip().lower()
            for login in os.getenv("ENG_PLATFORM_ALLOWED_GITHUB_LOGINS", "").split(",")
            if login.strip()
        ),
        trust_iap_identity=os.getenv("ENG_PLATFORM_TRUST_IAP_IDENTITY", "false").lower()
        == "true",
    )

    public_base_url = os.getenv("ENG_PLATFORM_MCP_PUBLIC_BASE_URL", "").rstrip("/")
    mcp = MCPConfig(
        enabled=os.getenv("ENG_PLATFORM_MCP_ENABLED", "false").lower() == "true",
        public_base_url=public_base_url,
        issuer_url=os.getenv("ENG_PLATFORM_MCP_ISSUER_URL", public_base_url).rstrip(
            "/"
        ),
        audit_collection=os.getenv(
            "ENG_PLATFORM_MCP_AUDIT_FIRESTORE_COLLECTION", "eng_platform_mcp_audit"
        ).strip(),
        oauth_collection=os.getenv(
            "ENG_PLATFORM_MCP_OAUTH_FIRESTORE_COLLECTION", "eng_platform_mcp_oauth"
        ).strip(),
        access_token_ttl_seconds=int(
            os.getenv("ENG_PLATFORM_MCP_ACCESS_TOKEN_TTL_SECONDS", "3600")
        ),
        refresh_token_ttl_seconds=int(
            os.getenv("ENG_PLATFORM_MCP_REFRESH_TOKEN_TTL_SECONDS", "2592000")
        ),
        mutation_limit_per_hour=int(
            os.getenv("ENG_PLATFORM_MCP_MUTATION_LIMIT_PER_HOUR", "10")
        ),
    )
    if mcp.enabled and not mcp.public_base_url:
        raise ValueError(
            "ENG_PLATFORM_MCP_PUBLIC_BASE_URL is required when MCP is enabled"
        )

    # Deprecated compatibility fields are inert; no Sonar credentials are loaded.
    sonarqube = SonarQubeConfig()

    quality = QualityConfig(
        ingest_token=os.getenv("ENG_PLATFORM_QUALITY_INGEST_TOKEN", ""),
        bucket=os.getenv("ENG_PLATFORM_QUALITY_BUCKET", ""),
        prefix=os.getenv("ENG_PLATFORM_QUALITY_PREFIX", "quality").strip("/"),
        local_store_path=os.getenv("ENG_PLATFORM_QUALITY_STORE_PATH", "data"),
        stale_after_hours=int(
            os.getenv("ENG_PLATFORM_QUALITY_STALE_AFTER_HOURS", "168")
        ),
    )

    return PlatformConfig(
        mock_mode=mock_mode,
        billing=billing,
        monitoring=monitoring,
        github=github,
        cloud_build=cloud_build,
        release_orchestrator=release_orchestrator,
        auth=auth,
        mcp=mcp,
        sonarqube=sonarqube,
        quality=quality,
    )


# Global config instance
config = load_config()
