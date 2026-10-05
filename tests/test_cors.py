"""Separate-origin database view retirement requires an authorized preflight."""

from fastapi.testclient import TestClient

from eng_platform_api.main import app


VIEW = (
    "/api/databases/sample/workspaces/"
    + "a" * 32
    + "/executions/"
    + "b" * 32
    + "/views/"
    + "c" * 32
)


def preflight(origin):
    return TestClient(app).options(
        VIEW,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "content-type,x-requested-with",
        },
    )


def test_configured_frontend_can_preflight_authenticated_view_delete():
    middleware = next(
        item for item in app.user_middleware if item.cls.__name__ == "CORSMiddleware"
    )
    origin = middleware.kwargs["allow_origins"][0]
    response = preflight(origin)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "DELETE" in response.headers["access-control-allow-methods"].split(", ")
    assert "x-requested-with" in response.headers["access-control-allow-headers"]


def test_unconfigured_origin_cannot_preflight_view_delete():
    response = preflight("https://untrusted.example")
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
