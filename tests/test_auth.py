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


def test_mock_login_and_logout_manage_the_operator_session():
    client = TestClient(app)
    with mock.patch("eng_platform_api.routers.auth.config.mock_mode", True):
        login = client.get(
            "/api/auth/login?next=http://localhost:5173/deployments/eng-platform-api",
            follow_redirects=False,
        )
        session = client.get("/api/auth/me")
        logout = client.post("/api/auth/logout")

    assert login.status_code == 307
    assert login.headers["location"].endswith("/deployments/eng-platform-api")
    assert session.json()["login"] == "diegomad14"
    assert logout.json()["authenticated"] is False


def test_github_oauth_login_and_callback_preserve_safe_destination():
    authorize_client = mock.MagicMock()
    authorize_client.create_authorization_url.return_value = (
        "https://github.com/login/oauth/authorize?state=fixed-state",
        "fixed-state",
    )
    token_client = mock.MagicMock()
    token_client.fetch_token = mock.AsyncMock(return_value={"access_token": "redacted"})
    authenticated_client = mock.MagicMock()
    user_response = mock.MagicMock()
    user_response.json.return_value = {
        "login": "diegomad14",
        "avatar_url": "https://avatars.example/diegomad14",
    }
    authenticated_client.get = mock.AsyncMock(return_value=user_response)

    client = TestClient(app, base_url="https://testserver")
    with (
        mock.patch("eng_platform_api.routers.auth.config.mock_mode", False),
        mock.patch("eng_platform_api.routers.auth._configured", return_value=True),
        mock.patch(
            "eng_platform_api.routers.auth.secrets.token_urlsafe",
            return_value="fixed-state",
        ),
        mock.patch(
            "eng_platform_api.routers.auth.AsyncOAuth2Client",
            side_effect=[authorize_client, token_client, authenticated_client],
        ),
    ):
        login = client.get(
            "/api/auth/login?next=http://localhost:5173/deployments/eng-platform-api",
            follow_redirects=False,
        )
        callback = client.get(
            "/api/auth/callback?code=oauth-code&state=fixed-state",
            follow_redirects=False,
        )
        session = client.get("/api/auth/me")

    assert login.headers["location"].startswith(
        "https://github.com/login/oauth/authorize"
    )
    assert callback.headers["location"].endswith("/deployments/eng-platform-api")
    assert session.json() == {
        "authenticated": True,
        "can_deploy": True,
        "login": "diegomad14",
        "avatar_url": "https://avatars.example/diegomad14",
    }
    user_response.raise_for_status.assert_called_once_with()


def test_github_callback_rejects_missing_oauth_state():
    with mock.patch("eng_platform_api.routers.auth._configured", return_value=True):
        response = TestClient(app).get(
            "/api/auth/callback?code=oauth-code&state=unexpected",
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid OAuth state"


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
