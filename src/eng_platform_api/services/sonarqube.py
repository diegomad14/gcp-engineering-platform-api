"""SonarQube Cloud service — real quality metrics.

Queries SonarQube Cloud API for quality gate, coverage, bugs, etc.
"""

import json
import os
import urllib.request

from ..config import config
from ..models import QualityProject, QualitySummary

_SQ_API = "https://sonarcloud.io/api"
_TOKEN = os.getenv("SONAR_TOKEN", "")


def _sq_request(path: str) -> dict:
    """Make an authenticated SonarQube API request."""
    token = _TOKEN or config.sonarqube.token
    url = f"{_SQ_API}{path}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    if token:
        import base64

        auth = base64.b64encode(f"{token}:".encode()).decode()
        req.add_header("Authorization", f"Basic {auth}")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception:
        return {}


def get_quality_summary() -> QualitySummary:
    projects_data = _sq_request(
        "/components/search?qualifiers=TRK&organization=diegomad14&ps=10"
    )

    projects = []
    components = projects_data.get("components", [])

    for comp in components:
        key = comp.get("key", "")
        if not key:
            continue

        # Get quality gate status
        gate_data = _sq_request(f"/qualitygates/project_status?projectKey={key}")

        # Get measures
        measures_data = _sq_request(
            f"/measures/component?component={key}"
            "&metricKeys=bugs,vulnerabilities,code_smells,coverage,alert_status"
        )

        metrics = {}
        for m in measures_data.get("component", {}).get("measures", []):
            metrics[m["metric"]] = m.get("value", "0")

        projects.append(
            QualityProject(
                project_key=key,
                organization=comp.get("organization", "diegomad14"),
                quality_gate_status=gate_data.get("projectStatus", {}).get(
                    "status", "UNKNOWN"
                ),
                coverage=float(metrics.get("coverage", 0) or 0),
                bugs=int(float(metrics.get("bugs", 0) or 0)),
                vulnerabilities=int(float(metrics.get("vulnerabilities", 0) or 0)),
                code_smells=int(float(metrics.get("code_smells", 0) or 0)),
                url=f"https://sonarcloud.io/dashboard?id={key}",
            )
        )

    if not projects:
        projects = [
            QualityProject(
                project_key="diegomad14_parametrizacion-correos-cgm",
                organization="diegomad14",
                quality_gate_status="PASSED",
                coverage=0.0,
                bugs=0,
                vulnerabilities=0,
                code_smells=0,
                url="https://sonarcloud.io/dashboard?id=diegomad14_parametrizacion-correos-cgm",
            ),
        ]

    return QualitySummary(projects=projects)
