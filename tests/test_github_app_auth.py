"""Migration must prefer renewable App credentials and never silently downgrade."""

from unittest import mock

import pytest

from eng_platform_api.config import GitHubConfig
from eng_platform_api.services import github_deployments


def test_complete_app_configuration_takes_precedence_over_legacy_token():
    configured = GitHubConfig(
        app_id="123", installation_id="456", private_key="private", token="legacy"
    )
    with (
        mock.patch.object(github_deployments.config, "github", configured),
        mock.patch.object(github_deployments, "GithubIntegration") as integration,
        mock.patch.object(github_deployments, "Github") as legacy,
    ):
        result = github_deployments.github_client()
    integration.assert_called_once_with(123, "private")
    integration.return_value.get_github_for_installation.assert_called_once_with(456)
    assert result is integration.return_value.get_github_for_installation.return_value
    legacy.assert_not_called()


@pytest.mark.parametrize("field", ["app_id", "installation_id", "private_key"])
def test_partial_app_configuration_does_not_fall_back_to_personal_token(field):
    configured = GitHubConfig(token="legacy")
    setattr(configured, field, "configured")
    with (
        mock.patch.object(github_deployments.config, "github", configured),
        mock.patch.object(github_deployments, "Github") as legacy,
    ):
        with pytest.raises(RuntimeError, match="incompletely configured"):
            github_deployments.github_client()
    legacy.assert_not_called()
