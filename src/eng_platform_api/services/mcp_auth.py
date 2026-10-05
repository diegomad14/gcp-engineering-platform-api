"""OAuth 2.1 provider for the remote MCP endpoint.

GitHub is deliberately only the upstream identity provider.  Tokens issued to
MCP clients are opaque, short-lived, scoped, and stored only by hash.
"""

from __future__ import annotations

import time
import secrets
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
from . import mcp_store, mcp_grants
from . import database_job_store as grant_store
from fastapi import HTTPException

_SCOPES = {mcp_grants.SCOPE}
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
        scopes = set((client_info.scope or "eng-platform.access").split())
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
        if (params.scopes or [mcp_grants.SCOPE]) != [mcp_grants.SCOPE]:
            raise AuthorizeError("invalid_scope", "Reconnect MCP for full access")
        mcp_grants.resource(params.resource)
        state = mcp_store.opaque_id()
        callback = _github_callback_url()
        mcp_store.save(
            "state",
            mcp_store.token_key(state),
            {
                "client_id": client.client_id,
                "scopes": params.scopes or ["eng-platform.access"],
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
        pending = mcp_store.consume("state", key)
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
        if not login or not database_login(login):
            raise AuthorizeError("access_denied", "GitHub identity is invalid")
        consent = mcp_store.opaque_id()
        mcp_store.save(
            "consent",
            mcp_store.token_key(consent),
            {**pending, "subject": login, "expires_at": time.time() + 300},
        )
        return f"{config.mcp.public_base_url}/mcp/consent?consent={consent}"

    @staticmethod
    def _authorization_redirect(pending: dict[str, Any]) -> str:
        code = mcp_store.opaque_id()
        mcp_store.save(
            "code",
            mcp_store.token_key(code),
            {**pending, "expires_at": time.time() + 120},
        )
        separator = "&" if "?" in pending["redirect_uri"] else "?"
        from urllib.parse import urlencode

        values = {"code": code}
        if pending.get("client_state") is not None:
            values["state"] = pending["client_state"]
        return f"{pending['redirect_uri']}{separator}{urlencode(values)}"

    def bind_consent_browser(self, destination: str) -> str | None:
        from urllib.parse import parse_qs

        prefix = f"{config.mcp.public_base_url}/mcp/consent?"
        if not destination.startswith(prefix):
            return None
        consent = parse_qs(urlparse(destination).query).get("consent", [""])[0]
        key = mcp_store.token_key(consent)
        pending = mcp_store.get("consent", key)
        if not pending or _expires_at(pending) < time.time():
            raise AuthorizeError("access_denied", "Consent is invalid or expired")
        nonce = mcp_store.opaque_id()
        mcp_store.save(
            "consent",
            key,
            {**pending, "browser_nonce_hash": mcp_store.token_key(nonce)},
        )
        return nonce

    def consent_record(self, consent: str, nonce: str) -> dict[str, Any] | None:
        pending = mcp_store.get("consent", mcp_store.token_key(consent))
        if (
            not pending
            or _expires_at(pending) < time.time()
            or not nonce
            or not secrets.compare_digest(
                str(pending.get("browser_nonce_hash", "")), mcp_store.token_key(nonce)
            )
        ):
            return None
        return pending

    def complete_consent(self, consent: str, nonce: str, allow: bool) -> str:
        from urllib.parse import urlencode

        if self.consent_record(consent, nonce) is None:
            raise AuthorizeError("access_denied", "Consent is invalid or expired")
        pending = mcp_store.consume_consent(mcp_store.token_key(consent))
        if not pending:
            raise AuthorizeError("access_denied", "Consent was already consumed")
        if allow:
            return self._authorization_redirect(pending)
        values = {"error": "access_denied"}
        if pending.get("client_state") is not None:
            values["state"] = pending["client_state"]
        separator = "&" if "?" in pending["redirect_uri"] else "?"
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
        grant_id: str | None = None,
        previous_refresh: str | None = None,
    ) -> OAuthToken:
        if scopes != [mcp_grants.SCOPE] or not subject or not database_login(subject):
            raise TokenError("invalid_grant", "Reconnect MCP for full access")
        canonical = mcp_grants.resource(resource)
        access, refresh = mcp_store.opaque_id(), mcp_store.opaque_id()
        identity = grant_id or mcp_store.token_key(mcp_store.opaque_id())
        old = mcp_grants.validate(identity, subject) if grant_id else None
        generation = old["generation"] + 1 if old else 1
        now = time.time()
        expires = now + config.mcp.refresh_token_ttl_seconds
        common = {
            "client_id": client_id,
            "scopes": scopes,
            "subject": subject,
            "resource": canonical,
            "session_id": identity,
            "generation": generation,
        }
        # Inactive indices are harmless if the atomic grant commit fails.
        mcp_store.save(
            "access",
            mcp_store.token_key(access),
            {**common, "expires_at": now + config.mcp.access_token_ttl_seconds},
        )
        mcp_store.save(
            "refresh", mcp_store.token_key(refresh), {**common, "expires_at": expires}
        )

        activation = {"accepted": False}

        def activate(values):
            activation["accepted"] = False
            current = values[f"session:{identity}"]
            if old:
                if (
                    not current
                    or current.get("revoked_at")
                    or current.get("generation") != old["generation"]
                    or current.get("refresh_hash") != previous_refresh
                    or current.get("expires_at", 0) <= time.time()
                    or current.get("client_id") != client_id
                ):
                    return {}
            elif current:
                return {}
            activation["accepted"] = True
            return {
                f"session:{identity}": {
                    **(current or {}),
                    "kind": "session",
                    "id": identity,
                    "source": "mcp",
                    "login": subject,
                    "client_id": client_id,
                    "resource": canonical,
                    "scopes": scopes,
                    "authority_policy": mcp_grants.POLICY,
                    "generation": generation,
                    "refresh_hash": mcp_store.token_key(refresh),
                    "access_hash": mcp_store.token_key(access),
                    "created_at": current["created_at"] if current else now,
                    "expires_at": expires,
                    "revoked_at": 0.0,
                }
            }

        grant_store.mutate([("session", identity)], activate)
        if not activation["accepted"]:
            mcp_store.delete("access", mcp_store.token_key(access))
            mcp_store.delete("refresh", mcp_store.token_key(refresh))
            raise TokenError("invalid_grant", "Refresh token already used or revoked")
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=config.mcp.access_token_ttl_seconds,
            scope=mcp_grants.SCOPE,
            refresh_token=refresh,
        )

    def _credential(self, kind: str, token: str) -> dict | None:
        record = mcp_store.get(kind, mcp_store.token_key(token))
        if (
            not record
            or _expires_at(record) <= time.time()
            or record.get("scopes") != [mcp_grants.SCOPE]
            or not record.get("session_id")
        ):
            return None
        try:
            grant = mcp_grants.validate(record["session_id"], record.get("subject"))
        except HTTPException:
            return None
        if (
            record.get("generation") != grant.get("generation")
            or record.get("client_id") != grant.get("client_id")
            or record.get("resource") != grant.get("resource")
            or grant.get(f"{kind}_hash") != mcp_store.token_key(token)
        ):
            return None
        return record

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        key = mcp_store.token_key(authorization_code.code)
        record = mcp_store.consume("code", key)
        if (
            not record
            or _expires_at(record) < time.time()
            or record.get("client_id") != client.client_id
        ):
            raise TokenError(
                "invalid_grant", "authorization code is invalid or already used"
            )
        return await self._issue_tokens(
            client_id=client.client_id or "",
            scopes=list(record["scopes"]),
            subject=record.get("subject"),
            resource=record.get("resource"),
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        record = self._credential("refresh", refresh_token)
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
        record = self._credential("refresh", refresh_token.token)
        if (
            not record
            or _expires_at(record) < time.time()
            or record.get("client_id") != client.client_id
        ):
            raise TokenError(
                "invalid_grant", "refresh token is invalid or already used"
            )
        if scopes != [mcp_grants.SCOPE]:
            raise TokenError("invalid_scope", "Full MCP access is required")
        return await self._issue_tokens(
            client_id=client.client_id or "",
            scopes=scopes,
            subject=record.get("subject"),
            resource=record.get("resource"),
            grant_id=record["session_id"],
            previous_refresh=key,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        record = self._credential("access", token)
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
        self,
        token: AccessToken | RefreshToken | str,
        token_type_hint: str | None = None,
    ) -> None:
        del token_type_hint
        await self.revoke_client_token(
            token if isinstance(token, str) else token.token,
            None if isinstance(token, str) else token.client_id,
        )

    async def revoke_client_token(self, token: str, client_id: str | None) -> None:
        """Revocation may use a retained credential; it never authenticates it.

        The HTTP caller authenticates the public client first. Lookup stays
        separate from access/refresh loaders so rotated tokens cannot regain
        access, but a disconnect racing with refresh still revokes the family.
        """
        for kind in ("access", "refresh"):
            record = mcp_store.get(kind, mcp_store.token_key(token))
            if not record or (
                client_id is not None and record.get("client_id") != client_id
            ):
                continue
            if record.get("session_id") and record.get("scopes") == [mcp_grants.SCOPE]:
                mcp_grants.revoke(record["session_id"])
                mcp_store.save_audit(
                    {
                        "subject": record.get("subject", ""),
                        "client_id": record["client_id"],
                        "tool": "revoke_mcp_connection",
                        "grant_id": record["session_id"],
                        "mutation": False,
                        "result": "revoked",
                    }
                )
                return
            mcp_store.delete(kind, mcp_store.token_key(token))


def database_login(value: str) -> bool:
    import re

    return re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})", value) is not None


provider = MCPAuthProvider()
