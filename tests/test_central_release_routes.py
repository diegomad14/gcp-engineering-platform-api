"""Internal endpoints never echo authentication, source URLs or provider errors."""

from io import BytesIO
from unittest.mock import Mock, patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from eng_platform_api.main import app
from eng_platform_api.routers import central_releases as routes

CLIENT = TestClient(app)
ROOT = "/api/internal/central-releases/"


@pytest.mark.parametrize(
    "body", [[], {}, {"token": 5}, {"token": "private", "project": "evil"}]
)
def test_bad_consume_payload_is_redacted(body):
    response = CLIENT.post(ROOT + "consume", json=body)
    assert response.status_code == 422
    assert "private" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_oversized_body_is_rejected():
    assert (
        CLIENT.post(ROOT + "consume", json={"token": "x" * 100001}).status_code == 422
    )


@pytest.mark.parametrize(
    "error,status",
    [
        (routes.release_plan.ReleasePlanError("private"), 409),
        (routes.release_authorization.ReleaseAuthorizationError("private"), 401),
        (RuntimeError("private"), 503),
    ],
)
def test_provider_and_authentication_errors_are_redacted(error, status):
    with patch.object(routes.central_releases, "consume", side_effect=error):
        response = CLIENT.post(ROOT + "consume", json={"token": "private"})
    assert response.status_code == status
    assert "private" not in response.text


def test_consume_forwards_oidc_and_returns_context():
    with patch.object(
        routes.central_releases, "consume", return_value={"execution_id": "safe"}
    ) as consume:
        response = CLIENT.post(
            ROOT + "consume",
            json={"token": "signed"},
            headers={"X-GitHub-OIDC": "identity"},
        )
    consume.assert_called_once_with("signed", "identity")
    assert response.json() == {"execution_id": "safe"}


def test_context_requires_bound_identity():
    with patch.object(
        routes.central_releases,
        "authorized_execution",
        return_value=(None, {"plan": {"safe": True}}),
    ):
        assert CLIENT.get(ROOT + str(uuid4()) + "/context").json() == {
            "plan": {"safe": True}
        }
    assert CLIENT.get(ROOT + "invalid/context").status_code == 503


def test_source_only_allows_github_archive_host():
    github = Mock()
    github.get_repo.return_value.get_archive_link.return_value = (
        "https://attacker.example/private"
    )
    with (
        patch.object(
            routes.central_releases,
            "authorized_execution",
            return_value=(
                None,
                {"plan": {"repository": "owner/source", "sha": "a" * 40}},
            ),
        ),
        patch.object(routes.github_deployments, "github_client", return_value=github),
        patch.object(routes, "urlopen") as download,
    ):
        response = CLIENT.get(ROOT + str(uuid4()) + "/source")
    assert response.status_code == 503
    download.assert_not_called()
    assert "attacker" not in response.text


def test_source_streams_without_disclosing_private_url():
    github = Mock()
    github.get_repo.return_value.get_archive_link.return_value = (
        "https://codeload.github.com/owner/source/private"
    )
    with (
        patch.object(
            routes.central_releases,
            "authorized_execution",
            return_value=(
                None,
                {"plan": {"repository": "owner/source", "sha": "a" * 40}},
            ),
        ),
        patch.object(routes.github_deployments, "github_client", return_value=github),
        patch.object(routes, "urlopen", return_value=BytesIO(b"archive")),
    ):
        response = CLIENT.get(ROOT + str(uuid4()) + "/source")
    assert response.content == b"archive"
    assert response.headers["cache-control"] == "no-store"


def test_result_accepts_only_sanitized_metadata():
    path = ROOT + str(uuid4()) + "/result"
    assert (
        CLIENT.post(
            path, json={"status": "FAILED", "secret_value": "private"}
        ).status_code
        == 422
    )
    with patch.object(routes.central_releases, "report") as report:
        assert CLIENT.post(path, json={"status": "FAILED"}).json() == {"accepted": True}
        report.assert_called_once()
    with patch.object(
        routes.central_releases, "report", side_effect=RuntimeError("private")
    ):
        response = CLIENT.post(path, json={"status": "FAILED"})
    assert response.status_code == 503
    assert "private" not in response.text


@pytest.mark.parametrize(
    "operation,body",
    [("progress", {"stage": "promote"}), ("checkpoint", {"services": {}, "jobs": {}})],
)
def test_runtime_boundaries_forward_only_oidc_authorized_metadata(operation, body):
    path = ROOT + str(uuid4()) + "/" + operation
    with patch.object(routes.central_releases, operation) as callback:
        response = CLIENT.post(path, json=body, headers={"X-GitHub-OIDC": "identity"})
    assert response.json() == {"accepted": True}
    assert callback.call_args.args[1] == "identity"
    with patch.object(
        routes.central_releases, operation, side_effect=RuntimeError("private")
    ):
        response = CLIENT.post(path, json=body)
    assert response.status_code == 503
    assert "private" not in response.text


def test_progress_rejects_arbitrary_fields():
    assert (
        CLIENT.post(
            ROOT + str(uuid4()) + "/progress",
            json={"stage": "promote", "value": "private"},
        ).status_code
        == 422
    )
