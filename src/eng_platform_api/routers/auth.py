"""GitHub OAuth session endpoints for platform operators."""

from __future__ import annotations

import hmac
import secrets
from typing import Annotated
from urllib.parse import urlparse

from authlib.integrations.httpx_client import (  # type: ignore[import-untyped]
    AsyncOAuth2Client,
)
from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse

from ..config import config
from ..models import AuthSession
from ..security import get_identity

router = APIRouter(prefix="/api/auth", tags=["auth"])

_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_TOKEN_URL = "https://github.com/login/oauth/access_token"
_USER_URL = "https://api.github.com/user"


def _configured() -> bool:
    return bool(
        config.auth.github_client_id
        and config.auth.github_client_secret
        and config.auth.session_secret
    )


def _safe_return_url(value: str) -> str:
    fallback = f"{config.auth.frontend_url}/deployments"
    if not value:
        return fallback
    requested = urlparse(value)
    frontend = urlparse(config.auth.frontend_url)
    if (requested.scheme, requested.netloc) != (frontend.scheme, frontend.netloc):
        return fallback
    return value


@router.get("/me", response_model=AuthSession)
async def current_session(request: Request):
    identity = get_identity(request)
    allowed = config.auth.allowed_logins
    can_deploy = identity != "anonymous" and (
        not allowed or identity.lower() in allowed
    )
    return AuthSession(
        authenticated=identity != "anonymous",
        can_deploy=can_deploy,
        login="" if identity == "anonymous" else identity,
        avatar_url=str(request.session.get("github_avatar_url", "")),
    )


@router.get("/login")
async def github_login(
    request: Request,
    next_url: Annotated[str, Query(alias="next")] = "",
):
    if config.mock_mode:
        request.session["github_login"] = "diegomad14"
        return RedirectResponse(_safe_return_url(next_url))
    if not _configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub OAuth is not configured",
        )
    state = secrets.token_urlsafe(32)
    request.session["oauth_state"] = state
    request.session["oauth_return_url"] = _safe_return_url(next_url)
    client = AsyncOAuth2Client(
        client_id=config.auth.github_client_id,
        redirect_uri=str(request.url_for("github_callback")),
    )
    authorization_url, _ = client.create_authorization_url(
        _AUTHORIZE_URL,
        state=state,
        scope="read:user",
    )
    return RedirectResponse(authorization_url)


@router.get("/callback", name="github_callback")
async def github_callback(request: Request):
    if not _configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GitHub OAuth is not configured",
        )
    expected_state = str(request.session.pop("oauth_state", ""))
    supplied_state = request.query_params.get("state", "")
    if not expected_state or not hmac.compare_digest(expected_state, supplied_state):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid OAuth state",
        )
    client = AsyncOAuth2Client(
        client_id=config.auth.github_client_id,
        client_secret=config.auth.github_client_secret,
        redirect_uri=str(request.url_for("github_callback")),
    )
    token = await client.fetch_token(
        _TOKEN_URL,
        authorization_response=str(request.url),
    )
    authenticated = AsyncOAuth2Client(token=token)
    response = await authenticated.get(
        _USER_URL,
        headers={"Accept": "application/vnd.github+json"},
    )
    response.raise_for_status()
    user = response.json()
    login = str(user.get("login", "")).strip()
    if not login:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="GitHub did not return a user login",
        )
    request.session["github_login"] = login
    request.session["github_avatar_url"] = str(user.get("avatar_url", ""))
    destination = str(
        request.session.pop(
            "oauth_return_url", f"{config.auth.frontend_url}/deployments"
        )
    )
    return RedirectResponse(destination)


@router.post("/logout", response_model=AuthSession)
async def logout(request: Request):
    request.session.clear()
    return AuthSession()
