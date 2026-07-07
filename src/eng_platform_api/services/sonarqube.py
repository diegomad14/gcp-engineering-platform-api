"""SonarQube Cloud service — quality metrics.

MVP: Returns mock data. Real SonarQube integration requires:
- SONAR_TOKEN configured
- SonarQube Cloud project established
"""

from ..config import config
from ..models import QualityProject, QualitySummary


def _mock_quality() -> list[QualityProject]:
    return [
        QualityProject(
            project_key="cgm-sanplat-param",
            organization="diegomad14",
            quality_gate_status="OK",
            coverage=72.5,
            bugs=3,
            vulnerabilities=0,
            code_smells=12,
            url="https://sonarcloud.io/project/overview?id=cgm-sanplat-param",
        ),
    ]


def get_quality_summary() -> QualitySummary:
    if config.mock_mode or not config.sonarqube.enabled:
        projects = _mock_quality()
    else:
        projects = _mock_quality()  # TODO: SonarQube API integration

    return QualitySummary(projects=projects)
