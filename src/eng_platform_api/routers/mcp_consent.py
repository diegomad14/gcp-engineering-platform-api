"""Browser-bound full MCP consent and client-bound connection revocation."""

import secrets
from html import escape
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from mcp.server.auth.provider import AuthorizeError
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.middleware.client_auth import (
    AuthenticationError,
    ClientAuthenticator,
)
from pydantic import BaseModel, Field, ValidationError
from typing import Literal

from ..config import config
from ..services.mcp_auth import provider
from ..services import mcp_grants, mcp_store

router = APIRouter()
_COOKIE = "eng_platform_mcp_consent"


@router.get("/authorize", include_in_schema=False)
async def authorize_connection(request: Request):
    """Migrate cached legacy scope requests into a fresh full-consent flow.

    Only old public registrations receive this redirect. The SDK still checks
    the registered redirect, PKCE and resource, and no credential is upgraded.
    """
    if not config.mcp.enabled:
        raise HTTPException(404)
    scopes = request.query_params.getlist("scope")
    legacy = {
        "eng-platform.read",
        "eng-platform.deploy",
        "eng-platform.cost-alerts.send",
    }
    requested = set(scopes[0].split()) if len(scopes) == 1 else set()
    if requested and requested.issubset(legacy):
        record = mcp_store.get("client", request.query_params.get("client_id", ""))
        metadata = (record or {}).get("metadata", {})
        registered = set((metadata.get("scope") or "").split())
        if (
            record
            and not record.get("revoked")
            and (not registered or registered.issubset(legacy))
        ):
            params = [
                (key, value)
                for key, value in request.query_params.multi_items()
                if key != "scope"
            ]
            params.append(("scope", mcp_grants.SCOPE))
            return RedirectResponse(
                "/authorize?" + urlencode(params),
                status_code=302,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
    return await AuthorizationHandler(provider).handle(request)


class _RevocationRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    token_type_hint: Literal["access_token", "refresh_token"] | None = None


@router.post("/revoke", include_in_schema=False)
async def revoke_connection(request: Request):
    """RFC 7009 disconnect, including a credential rotated concurrently.

    Authenticate the registered client using the SDK, but do not use its active
    token loaders: an inactive retained token is usable only to revoke its own
    family. Public PKCE clients do not need to supply a client secret.
    """
    if not config.mcp.enabled:
        raise HTTPException(404)
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    try:
        client = await ClientAuthenticator(provider).authenticate_request(request)
    except AuthenticationError:
        return JSONResponse(
            {"error": "unauthorized_client"}, status_code=401, headers=headers
        )
    try:
        payload = _RevocationRequest.model_validate(dict(await request.form()))
    except ValidationError:
        return JSONResponse(
            {"error": "invalid_request"}, status_code=400, headers=headers
        )
    await provider.revoke_client_token(payload.token, client.client_id)
    return Response(status_code=200, headers=headers)


def _pending(request: Request, consent: str):
    if not config.mcp.enabled:
        raise HTTPException(404)
    pending = provider.consent_record(consent, request.cookies.get(_COOKIE, ""))
    if pending is None:
        raise HTTPException(403, "Consent is invalid or expired")
    return pending


@router.get("/mcp/consent", include_in_schema=False)
async def consent_page(request: Request, consent: str = ""):
    pending = _pending(request, consent)
    client = await provider.get_client(pending["client_id"])
    if client is None:
        raise HTTPException(403, "OAuth client is no longer available")
    labels = {
        "eng-platform.access": (
            "Acceso completo: consultar costes, métricas y estado; ejecutar consultas "
            "BD de solo lectura sobre todas las bases habilitadas; desplegar y revertir "
            "servicios de producción; activar alertas al destinatario privado configurado de Diego"
        ),
    }
    permissions = "".join(
        f"<li>{escape(labels[scope])}</li>" for scope in pending["scopes"]
    )
    # Chromium applies form-action to the POST's redirect as well. This origin
    # comes from the SDK-validated, registered redirect, never from form input.
    callback = urlparse(pending["redirect_uri"])
    callback_origin = f"{callback.scheme}://{callback.netloc}"
    page = (
        '<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
        "<title>Autorizar Engineering Platform</title><main><h1>Autorizar Engineering Platform</h1>"
        f"<p>Aplicación: {escape(client.client_name or 'Cliente MCP')}. Cuenta: {escape(pending['subject'])}.</p>"
        f"<p>Permisos solicitados:</p><ul>{permissions}</ul>"
        "<p>Esta conexión podrá usar todas las acciones disponibles. Las consultas SQL siguen siendo de solo lectura. Desconectar revoca el acceso y las capturas privadas.</p>"
        '<form method="post" action="/mcp/consent">'
        f'<input type="hidden" name="consent" value="{escape(consent, quote=True)}">'
        f'<input type="hidden" name="csrf" value="{escape(request.cookies[_COOKIE], quote=True)}">'
        '<button name="decision" value="allow">Autorizar</button> '
        '<button name="decision" value="deny">Cancelar</button></form></main></html>'
    )
    return HTMLResponse(
        page,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": f"default-src 'none'; form-action 'self' {callback_origin}; frame-ancestors 'none'; base-uri 'none'",
            "X-Frame-Options": "DENY",
        },
    )


@router.post("/mcp/consent", include_in_schema=False)
async def consent_decision(request: Request):
    if not config.mcp.enabled:
        raise HTTPException(404)
    origin = urlparse(config.mcp.public_base_url)
    if request.headers.get("origin") != f"{origin.scheme}://{origin.netloc}":
        raise HTTPException(403, "Consent origin is invalid")
    form = await request.form()
    consent, csrf = str(form.get("consent", "")), str(form.get("csrf", ""))
    _pending(request, consent)
    if not csrf or not secrets.compare_digest(csrf, request.cookies.get(_COOKIE, "")):
        raise HTTPException(403, "Consent verification failed")
    decision = form.get("decision")
    if decision not in {"allow", "deny"}:
        raise HTTPException(400, "Choose authorize or cancel")
    try:
        destination = provider.complete_consent(consent, csrf, decision == "allow")
    except AuthorizeError as exc:
        raise HTTPException(403, "Consent is invalid or already consumed") from exc
    response = RedirectResponse(
        destination,
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
    response.delete_cookie(_COOKIE, path="/mcp/consent")
    return response
