"""Browser-bound consent for explicitly requested private cost-alert permission."""

import secrets
from html import escape
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from mcp.server.auth.provider import AuthorizeError

from ..config import config
from ..services.mcp_auth import provider

router = APIRouter()
_COOKIE = "eng_platform_mcp_consent"


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
            "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'",
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
