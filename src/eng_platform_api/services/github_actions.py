"""GitHub Actions service — release history and workflow status.

MVP: Returns mock data. Real GitHub integration requires:
- GitHub token
- App repo access
"""

from ..config import config
from ..models import ReleaseItem, ReleaseSummary


def _mock_releases() -> list[ReleaseItem]:
    return [
        ReleaseItem(
            app_id="cgm-integration-platform",
            app_name="CGM Integration Platform",
            version="v0.4.3",
            status="promoted",
            api_revision="cgm-sanplat-api-00179-tad",
            web_revision="cgm-sanplat-web-00080-g8c",
            github_run_url="https://github.com/diegomad14/parametrizacion-correos-cgm/actions/runs/example",
            created_at="2026-07-05T14:30:00Z",
        ),
        ReleaseItem(
            app_id="cgm-integration-platform",
            app_name="CGM Integration Platform",
            version="v0.5.0",
            status="candidate",
            api_revision="cgm-sanplat-api-00185-xyz",
            web_revision="",
            github_run_url="https://github.com/diegomad14/parametrizacion-correos-cgm/actions/runs/example2",
            created_at="2026-07-07T09:15:00Z",
        ),
    ]


def get_release_summary() -> ReleaseSummary:
    if config.mock_mode or not config.github.enabled:
        recent = _mock_releases()
    else:
        recent = _mock_releases()  # TODO: GitHub API integration

    return ReleaseSummary(recent=recent, total_releases=len(recent))
