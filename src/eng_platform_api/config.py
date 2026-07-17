"""Configuration for the Engineering Platform API.

All integrations default to mock mode. Real GCP/GitHub/SonarQube
integrations require explicit environment variable configuration.
"""

import os
from dataclasses import dataclass, field


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


@dataclass
class AuthConfig:
    github_client_id: str = ""
    github_client_secret: str = ""
    session_secret: str = ""
    frontend_url: str = "http://localhost:5173"
    allowed_logins: tuple[str, ...] = ()


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
    auth: AuthConfig = field(default_factory=AuthConfig)
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
        token=os.getenv("ENG_PLATFORM_GITHUB_TOKEN", ""),
        app_id=os.getenv("ENG_PLATFORM_GITHUB_APP_ID", ""),
        installation_id=os.getenv("ENG_PLATFORM_GITHUB_INSTALLATION_ID", ""),
        private_key=os.getenv("ENG_PLATFORM_GITHUB_PRIVATE_KEY", "").replace(
            "\\n", "\n"
        ),
        deployment_workflow=os.getenv(
            "ENG_PLATFORM_GITHUB_DEPLOYMENT_WORKFLOW", "platform-deploy.yml"
        ),
    )

    auth = AuthConfig(
        github_client_id=os.getenv("ENG_PLATFORM_GITHUB_OAUTH_CLIENT_ID", ""),
        github_client_secret=os.getenv("ENG_PLATFORM_GITHUB_OAUTH_CLIENT_SECRET", ""),
        session_secret=os.getenv("ENG_PLATFORM_SESSION_SECRET", ""),
        frontend_url=os.getenv(
            "ENG_PLATFORM_FRONTEND_URL", "http://localhost:5173"
        ).rstrip("/"),
        allowed_logins=tuple(
            login.strip().lower()
            for login in os.getenv(
                "ENG_PLATFORM_ALLOWED_GITHUB_LOGINS", "diegomad14"
            ).split(",")
            if login.strip()
        ),
    )

    sonarqube = SonarQubeConfig(
        enabled=os.getenv("ENG_PLATFORM_SONARQUBE_ENABLED", "false").lower() == "true",
        token=os.getenv("ENG_PLATFORM_SONARQUBE_TOKEN", ""),
        host_url=os.getenv("SONAR_HOST_URL", "https://sonarcloud.io"),
    )

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
        auth=auth,
        sonarqube=sonarqube,
        quality=quality,
    )


# Global config instance
config = load_config()
