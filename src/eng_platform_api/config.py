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
    app_label: str = "app"
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


@dataclass
class SonarQubeConfig:
    enabled: bool = False
    token: str = ""
    host_url: str = "https://sonarcloud.io"


@dataclass
class PlatformConfig:
    mock_mode: bool = True
    billing: BillingConfig = field(default_factory=BillingConfig)
    monitoring: MonitoringConfig = field(default_factory=MonitoringConfig)
    github: GitHubConfig = field(default_factory=GitHubConfig)
    sonarqube: SonarQubeConfig = field(default_factory=SonarQubeConfig)


def load_config() -> PlatformConfig:
    """Load configuration from environment variables."""

    mock_mode = os.getenv("ENG_PLATFORM_MOCK_MODE", "true").lower() == "true"

    billing = BillingConfig(
        enabled=os.getenv("ENG_PLATFORM_BILLING_ENABLED", "false").lower() == "true",
        bigquery_project_id=os.getenv("ENG_PLATFORM_BQ_PROJECT_ID", ""),
        bigquery_dataset=os.getenv("ENG_PLATFORM_BQ_DATASET", "billing_export"),
        bigquery_table=os.getenv("ENG_PLATFORM_BQ_TABLE", "gcp_billing_export_resource_v1_XXXXXX"),
        bigquery_location=os.getenv("ENG_PLATFORM_BQ_LOCATION", "US"),
        app_label=os.getenv("ENG_PLATFORM_LABEL_APP", "app"),
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
    )

    sonarqube = SonarQubeConfig(
        enabled=os.getenv("ENG_PLATFORM_SONARQUBE_ENABLED", "false").lower() == "true",
        token=os.getenv("ENG_PLATFORM_SONARQUBE_TOKEN", ""),
        host_url=os.getenv("SONAR_HOST_URL", "https://sonarcloud.io"),
    )

    return PlatformConfig(
        mock_mode=mock_mode,
        billing=billing,
        monitoring=monitoring,
        github=github,
        sonarqube=sonarqube,
    )


# Global config instance
config = load_config()
