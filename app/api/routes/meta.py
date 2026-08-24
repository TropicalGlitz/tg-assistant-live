"""Webhooks de Meta + conexión de cuentas + acciones del panel para IG/FB.

El receptor de webhooks es público por fuerza (Meta le pega desde sus
servidores), así que la única defensa es la firma: se valida
X-Hub-Signature-256 contra el cuerpo CRUDO con el App Secret. Sin firma válida
no se procesa nada.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.chat import _admin_locked, _form_data
from app.core.config import get_settings
from app.db.session import get_session
from app.services import meta, meta_comments

_log = logging.getLogger("meta_routes")
_settings = get_settings()
router = APIRouter()

_pending_states: dict[str, str] = {}


def _auth_ok(key: str) -> bool:
    return bool(_settings.admin_token) and key == _settings.admin_token


def _back(key: str, msg: str = "", bad: bool = False, tab: str = "pending"):
    from urllib.parse import quote
    url = f"/admin/youtube?key={key}&tab={tab}"
    if msg:
        url += f"&msg={quote(msg)}&bad={'1' if bad else '0'}"
    return RedirectResponse(url, status_code=303)


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #

@router.get("/webhooks/meta")
async def meta_verify(request: Request) -> Response:
    """Handshake de verificación. Meta manda hub.challenge y espera el eco."""
    p = request.query_params
    if (
        p.get("hub.mode") == "subscribe"
        and _settings.meta_verify_token
        and p.get("hub.verify_token") == _settings.meta_verify_token
    ):
        return PlainTextResponse(p.get("hub.challenge", ""))
    _log.warning("Verificación de webhook de Meta rechazada")
    return PlainTextResponse("forbidden", status_code=status.HTTP_403_FORBIDDEN)


@router.post("/webhooks/meta")
async def meta_webhook(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    raw = await request.body()
    if not meta.verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        _log.warning("Webhook de Meta con firma inválida; se descarta")
        return Response(status_code=status.HTTP_401_UNAUTHORIZED)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return Response(status_code=status.HTTP_400_BAD_REQUEST)

    try:
        n = await meta_comments.ingest(session, payload)
        if n:
            _log.info("Meta: %s comentarios nuevos", n)
    except Exception:  # noqa: BLE001
        # Nunca devolvemos error a Meta por un fallo nuestro: reintentaría en
        # bucle. Lo registramos y seguimos.
        _log.exception("Falló procesar el webhook de Meta")
    return Response(status_code=status.HTTP_200_OK)


# --------------------------------------------------------------------------- #
# Conexión
# --------------------------------------------------------------------------- #

@router.post("/admin/meta/connect")
async def meta_connect(request: Request):
    form = await _form_data(request)
    key = form.get("key", "")
    if not _auth_ok(key):
        return HTMLResponse(_admin_locked("Token inválido."), 401)
    if not meta.configured():
        return _back(key, "Faltan META_APP_ID y META_APP_SECRET en Render.", True)
    nonce = secrets.token_urlsafe(24)
    _pending_states[nonce] = key
    return RedirectResponse(meta.auth_url(nonce), status_code=303)


@router.get("/admin/meta/callback")
async def meta_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    session: AsyncSession = Depends(get_session),
):
    key = _pending_states.pop(state, "")
    if not key or not _auth_ok(key):
        return HTMLResponse(_admin_locked("Sesión de conexión no válida."), 401)
    if error or not code:
        return _back(key, f"Meta canceló la conexión: {error or 'sin código'}", True)
    try:
        pages = await meta.exchange_code(session, code)
        subscribed = await meta.subscribe_pages(session)
    except Exception as exc:  # noqa: BLE001
        _log.exception("Falló la conexión con Meta")
        return _back(key, str(exc)[:300], True)
    igs = [p["ig_username"] for p in pages if p.get("ig_username")]
    msg = f"Conectado: {len(pages)} página(s)"
    if igs:
        msg += f" · Instagram: {', '.join(igs)}"
    if subscribed:
        msg += f" · suscrito a {len(subscribed)}"
    return _back(key, msg)


@router.post("/admin/meta/disconnect")
async def meta_disconnect(request: Request, session: AsyncSession = Depends(get_session)):
    form = await _form_data(request)
    key = form.get("key", "")
    if not _auth_ok(key):
        return HTMLResponse(_admin_locked("Token inválido."), 401)
    await meta.disconnect(session)
    return _back(key, "Cuentas de Meta desconectadas.")


@router.post("/admin/meta/subscribe")
async def meta_subscribe(request: Request, session: AsyncSession = Depends(get_session)):
    form = await _form_data(request)
    key = form.get("key", "")
    if not _auth_ok(key):
        return HTMLResponse(_admin_locked("Token inválido."), 401)
    try:
        done = await meta.subscribe_pages(session)
    except Exception as exc:  # noqa: BLE001
        return _back(key, str(exc)[:300], True)
    return _back(key, f"Suscritas {len(done)} página(s) a comentarios.")


# --------------------------------------------------------------------------- #
# Acciones sobre un comentario
# --------------------------------------------------------------------------- #

@router.post("/admin/meta/approve")
async def meta_approve(request: Request, session: AsyncSession = Depends(get_session)):
    form = await _form_data(request)
    key = form.get("key", "")
    if not _auth_ok(key):
        return HTMLResponse(_admin_locked("Token inválido."), 401)
    cid = form.get("comment_id", "")
    body = (form.get("body") or "").strip()
    if not cid or not body:
        return _back(key, "Falta el comentario o la respuesta está vacía.", True)
    r = await meta_comments.routing(session, cid)
    try:
        reply_id = await meta.reply_to_comment(
            session, cid, body,
            page_id=r.get("page_id", ""), ig_id=r.get("ig_id", ""),
        )
    except Exception as exc:  # noqa: BLE001
        _log.exception("Falló publicar la respuesta en Meta")
        return _back(key, str(exc)[:300], True)
    await meta_comments.mark_replied(session, cid, body, reply_id)
    return _back(key, "Respuesta publicada.")


@router.post("/admin/meta/skip")
async def meta_skip(request: Request, session: AsyncSession = Depends(get_session)):
    form = await _form_data(request)
    key = form.get("key", "")
    if not _auth_ok(key):
        return HTMLResponse(_admin_locked("Token inválido."), 401)
    cid = form.get("comment_id", "")
    if cid:
        await meta_comments.set_status(session, cid, "skipped")
    return _back(key, "Comentario descartado.")


@router.post("/admin/meta/regenerate")
async def meta_regenerate(request: Request, session: AsyncSession = Depends(get_session)):
    form = await _form_data(request)
    key = form.get("key", "")
    if not _auth_ok(key):
        return HTMLResponse(_admin_locked("Token inválido."), 401)
    cid = form.get("comment_id", "")
    if not cid:
        return _back(key, "Falta el comentario.", True)
    try:
        await meta_comments.regenerate(session, cid)
    except Exception as exc:  # noqa: BLE001
        return _back(key, str(exc)[:300], True)
    return _back(key, "Borrador regenerado.")
