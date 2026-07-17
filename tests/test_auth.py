"""Authentication boundary tests for deployment writes."""

from unittest import mock

from fastapi.testclient import TestClient

from eng_platform_api.main import app
from eng_platform_api.routers.auth import _safe_return_url


def test_session_is_anonymous_without_oauth_cookie():
    with mock.patch("eng_platform_api.security.config.mock_mode", False):
        response = TestClient(app).get("/api/auth/me")
    assert response.status_code == 200
    assert response.json() == {
        "authenticated": False,
        "can_deploy": False,
        "login": "",
        "avatar_url": "",
    }


def test_mock_session_is_ready_for_local_ux():
    with mock.patch("eng_platform_api.security.config.mock_mode", True):
        response = TestClient(app).get("/api/auth/me")
    assert response.json()["login"] == "diegomad14"
    assert response.json()["can_deploy"] is True


def test_oauth_return_url_cannot_leave_frontend_origin():
    assert _safe_return_url("https://attacker.example/collect") == (
        "http://localhost:5173/deployments"
    )


def test_deployment_write_requires_an_authenticated_operator():
    with mock.patch("eng_platform_api.security.config.mock_mode", False):
        response = TestClient(app).post(
            "/api/services/eng-platform-api/deployments",
            json={"tag": "v0.5.0"},
        )
    assert response.status_code == 401
