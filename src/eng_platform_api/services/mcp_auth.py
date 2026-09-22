"""OAuth 2.1 provider for the remote MCP endpoint.

GitHub is deliberately only the upstream identity provider.  Tokens issued to
MCP clients are opaque, short-lived, scoped, and stored only by hash.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

from authlib.integrations.httpx_client import AsyncOAuth2Client
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from ..config import config
from . import mcp_store

_SCOPES = {"eng-platform.read", "eng-platform.deploy", "eng-platform.rollback"}
_GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
_GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
_GITHUB_USER_URL = "https://api.github.com/user"


def _is_safe_redirect_uri(uri: str) -> bool:
    parsed = urlparse(uri)
    if parsed.fragment or parsed.username or parsed.password or not parsed.hostname:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in {
        "localhost",
        "127.0.0.1",
        "::1",
    }


def _expires_at(record: dict[str, Any]) -> float:
    return float(record.get("expires_at", 0))


def _github_callback_url() -> str:
    """Reuse the existing GitHub OAuth callback registered for the API."""
    return f"{config.mcp.public_base_url}/api/auth/callback"


def owns_pending_state(state: str) -> bool:
    """Whether a GitHub callback state belongs to a live MCP authorization."""
    pending = mcp_store.get("state", mcp_store.token_key(state))
    return bool(pending and _expires_at(pending) >= time.time())


class MCPAuthProvider:
    """MCP SDK OAuth provider backed by Firestore (or isolated mock memory)."""

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        record = mcp_store.get("client", client_id)
        if not record or record.get("revoked"):
            return None
        return OAuthClientInformationFull.model_validate(record["metadata"])

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        # Public/native DCR clients with Authorization Code + PKCE must not make
        # the platform persist a second long-lived client secret.
        if client_info.token_endpoint_auth_method != "none":
            raise RegistrationError(
                "invalid_client_metadata",
                "Only public PKCE clients (token_endpoint_auth_method=none) are supported",
            )
        if not client_info.client_id or not client_info.redirect_uris:
            raise RegistrationError(
                "invalid_client_metadata", "client_id and redirect_uris are required"
            )
        if not all(
            _is_safe_redirect_uri(str(uri)) for uri in client_info.redirect_uris
        ):
            raise RegistrationError(
                "invalid_redirect_uri", "redirect_uri must be HTTPS or localhost HTTP"
            )
        if not {"authorization_code", "refresh_token"}.issubset(
            set(client_info.grant_types)
        ):
            raise RegistrationError(
                "invalid_client_metadata",
                "authorization_code and refresh_token are required",
            )
        scopes = set((client_info.scope or "eng-platform.read").split())
        if not scopes or not scopes.issubset(_SCOPES):
            raise RegistrationError(
                "invalid_client_metadata", "Requested scopes are not supported"
            )
        mcp_store.save(
            "client",
            client_info.client_id,
            {"metadata": client_info.model_dump(mode="json")},
        )

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if not config.auth.github_client_id or not config.auth.github_client_secret:
            raise AuthorizeError(
                "temporarily_unavailable", "GitHub OAuth is not configured"
            )
        if not config.mcp.public_base_url:
            raise AuthorizeError("server_error", "MCP public URL is not configured")
        state = mcp_store.opaque_id()
        callback = _github_callback_url()
        mcp_store.save(
            "state",
            mcp_store.token_key(state),
            {
                "client_id": client.client_id,
                "scopes": params.scopes or ["eng-platform.read"],
                "code_challenge": params.code_challenge,
                "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "resource": params.resource,
                "client_state": params.state,
                "expires_at": time.time() + 600,
            },
        )
        github = AsyncOAuth2Client(
            client_id=config.auth.github_client_id, redirect_uri=callback
        )
        url, _ = github.create_authorization_url(
            _GITHUB_AUTHORIZE_URL, state=state, scope="read:user"
        )
        return url

    async def complete_github_authorization(
        self, *, state: str, authorization_response: str
    ) -> str:
        """Turn a verified GitHub identity into a one-time MCP authorization code."""
        key = mcp_store.token_key(state)
        pending = mcp_store.get("state", key)
        mcp_store.delete("state", key)
        if not pending or _expires_at(pending) < time.time():
            raise AuthorizeError("access_denied", "OAuth state is invalid or expired")
        callback = _github_callback_url()
        client = AsyncOAuth2Client(
            client_id=config.auth.github_client_id,
            client_secret=config.auth.github_client_secret,
            redirect_uri=callback,
        )
        token = await client.fetch_token(
            _GITHUB_TOKEN_URL, authorization_response=authorization_response
        )
        upstream = AsyncOAuth2Client(token=token)
        response = await upstream.get(
            _GITHUB_USER_URL, headers={"Accept": "application/vnd.github+json"}
        )
        response.raise_for_status()
        login = str(response.json().get("login", "")).strip().lower()
        if not login or login not in config.auth.allowed_logins:
            raise AuthorizeError(
                "access_denied", "GitHub user is not allowed to access eng-platform"
            )
        code = mcp_store.opaque_id()
        mcp_store.save(
            "code",
            mcp_store.token_key(code),
            {**pending, "subject": login, "expires_at": time.time() + 120},
        )
        separator = "&" if "?" in pending["redirect_uri"] else "?"
        from urllib.parse import urlencode

        values = {"code": code}
        if pending.get("client_state") is not None:
            values["state"] = pending["client_state"]
        return f"{pending['redirect_uri']}{separator}{urlencode(values)}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        record = mcp_store.get("code", mcp_store.token_key(authorization_code))
        if (
            not record
            or _expires_at(record) < time.time()
            or record.get("client_id") != client.client_id
        ):
            return None
        return AuthorizationCode(
            code=authorization_code,
            scopes=list(record["scopes"]),
            expires_at=_expires_at(record),
            client_id=client.client_id or "",
            code_challenge=str(record["code_challenge"]),
            redirect_uri=AnyUrl(record["redirect_uri"]),
            redirect_uri_provided_explicitly=bool(
                record.get("redirect_uri_provided_explicitly", True)
            ),
            resource=record.get("resource"),
            subject=record.get("subject"),
        )

    async def _issue_tokens(
        self,
        *,
        client_id: str,
        scopes: list[str],
        subject: str | None,
        resource: str | None,
    ) -> OAuthToken:
        access = mcp_store.opaque_id()
        refresh = mcp_store.opaque_id()
        session_id = mcp_store.opaque_id()
        mcp_store.save(
            "access",
            mcp_store.token_key(access),
            {
                "client_id": client_id,
                "scopes": scopes,
                "subject": subject,
                "resource": resource,
                "session_id": session_id,
                "expires_at": time.time() + config.mcp.access_token_ttl_seconds,
            },
        )
        mcp_store.save(
            "refresh",
            mcp_store.token_key(refresh),
            {
                "client_id": client_id,
                "scopes": scopes,
                "subject": subject,
                "resource": resource,
                "session_id": session_id,
                "expires_at": time.time() + config.mcp.refresh_token_ttl_seconds,
            },
        )
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=config.mcp.access_token_ttl_seconds,
            scope=" ".join(scopes),
            refresh_token=refresh,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        key = mcp_store.token_key(authorization_code.code)
        record = mcp_store.get("code", key)
        if (
            not record
            or _expires_at(record) < time.time()
            or record.get("client_id") != client.client_id
        ):
            raise TokenError(
                "invalid_grant", "authorization code is invalid or already used"
            )
        mcp_store.delete("code", key)
        return await self._issue_tokens(
            client_id=client.client_id or "",
            scopes=authorization_code.scopes,
            subject=authorization_code.subject,
            resource=authorization_code.resource,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        record = mcp_store.get("refresh", mcp_store.token_key(refresh_token))
        if (
            not record
            or _expires_at(record) < time.time()
            or record.get("client_id") != client.client_id
        ):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id or "",
            scopes=list(record["scopes"]),
            expires_at=int(_expires_at(record)),
            resource=record.get("resource"),
            subject=record.get("subject"),
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        key = mcp_store.token_key(refresh_token.token)
        record = mcp_store.get("refresh", key)
        if (
            not record
            or _expires_at(record) < time.time()
            or record.get("client_id") != client.client_id
        ):
            raise TokenError(
                "invalid_grant", "refresh token is invalid or already used"
            )
        mcp_store.delete("refresh", key)
        # Rotation invalidates the previous access token session as well.
        session_id = record.get("session_id")
        if session_id:
            mcp_store.delete_access_session(str(session_id))
        return await self._issue_tokens(
            client_id=client.client_id or "",
            scopes=scopes,
            subject=record.get("subject"),
            resource=record.get("resource"),
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        record = mcp_store.get("access", mcp_store.token_key(token))
        if not record or _expires_at(record) < time.time():
            return None
        return AccessToken(
            token=token,
            client_id=str(record["client_id"]),
            scopes=list(record["scopes"]),
            expires_at=int(_expires_at(record)),
            resource=record.get("resource"),
            subject=record.get("subject"),
        )

    async def verify_token(self, token: str) -> AccessToken | None:
        return await self.load_access_token(token)

    async def revoke_token(
        self, token: str, token_type_hint: str | None = None
    ) -> None:
        del token_type_hint
        for kind in ("access", "refresh"):
            record = mcp_store.get(kind, mcp_store.token_key(token))
            mcp_store.delete(kind, mcp_store.token_key(token))
            if record and record.get("session_id"):
                mcp_store.delete_access_session(str(record["session_id"]))


provider = MCPAuthProvider()
