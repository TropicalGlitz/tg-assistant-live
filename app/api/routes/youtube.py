"""Panel de comentarios de YouTube: revisar el borrador y publicar con un clic.

Nada se publica automáticamente. El sondeo deja los borradores en estado
`pending` y aquí un humano aprueba, edita o descarta. Las acciones van por POST
con el token en un campo oculto, no en la URL.
"""
from __future__ import annotations

import html
import logging
import secrets

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.chat import _admin_locked, _admin_page, _fmt_time, _form_data
from app.core.config import get_settings
from app.db.session import get_session
from app.services import yt_comments, youtube

_log = logging.getLogger("yt_routes")
_settings = get_settings()
router = APIRouter()

# nonce -> admin key. Solo vive mientras dura el ida y vuelta con Google.
_pending_states: dict[str, str] = {}

_TABS = (
    ("pending", "Por responder"),
    ("replied", "Respondidos"),
    ("skipped", "Descartados"),
    ("archived", "Ruido"),
    ("all", "Todos"),
)

_KIND_LABEL = {
    "question": ("Pregunta", "#dbeafe", "#1e40af"),
    "buying": ("Intención de compra", "#dcfce7", "#166534"),
    "praise": ("Elogio", "#fef3c7", "#92400e"),
    "spam": ("Spam", "#fee2e2", "#991b1b"),
    "other": ("Otro", "#ede9fe", "#6d28d9"),
    "pending": ("Sin clasificar", "#f1f5f9", "#475569"),
}


def _css() -> str:
    return (
        "<style>"
        ".ytc{background:#fff;border:1px solid #eee;border-radius:14px;padding:14px 16px;margin-bottom:14px}"
        ".ytc .top{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap;margin-bottom:8px}"
        ".ytc .who{font-weight:700;font-size:14px}"
        ".ytc .vid{font-size:12px;color:#6b6b74}"
        ".ytc .when{font-size:11px;color:#9a9aa2;margin-left:auto}"
        ".ytc .body{white-space:pre-wrap;background:#f8f8fa;border-radius:10px;padding:10px 12px;"
        "font-size:14px;margin-bottom:10px}"
        ".ytc textarea{width:100%;min-height:110px;border:1px solid #d7d7de;border-radius:10px;"
        "padding:10px 12px;font-size:14px;font-family:inherit;resize:vertical;line-height:1.45}"
        ".ytc .acts{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap;align-items:center}"
        ".ytc button{border:0;border-radius:999px;padding:9px 16px;font-size:13px;font-weight:700;cursor:pointer}"
        ".ytc .go{background:#ef2c8f;color:#fff}"
        ".ytc .sec2{background:#fff;color:#1b1b1f;border:1px solid #d7d7de}"
        ".ytc .warn{background:#fff;color:#b42318;border:1px solid #f3c4c0}"
        ".ytc .nodraft{color:#9a9aa2;font-size:13px;font-style:italic}"
        ".kind{border-radius:999px;padding:2px 9px;font-size:11px;font-weight:700}"
        ".flash{background:#e8f7ee;border:1px solid #b7e4c7;color:#166534;border-radius:10px;"
        "padding:10px 14px;margin-bottom:14px;font-size:13px}"
        ".flash.bad{background:#fdecec;border-color:#f3c4c0;color:#b42318}"
        ".conn{background:#fff;border:1px solid #eee;border-radius:14px;padding:14px 16px;margin-bottom:16px}"
        ".conn b{font-size:14px}"
        ".conn .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:10px}"
        ".conn button{border:0;border-radius:999px;padding:9px 16px;font-size:13px;font-weight:700;cursor:pointer}"
        ".conn .go{background:#ef2c8f;color:#fff}"
        ".conn .ghost{background:#fff;border:1px solid #d7d7de;color:#1b1b1f}"
        "</style>"
    )


def _connect_box(kf: str, conn: dict | None) -> str:
    if not youtube.configured():
        return (
            "<div class='conn'><b>Falta configurar Google</b>"
            "<div class='note' style='margin-top:6px'>Agrega <code>YOUTUBE_CLIENT_ID</code> y "
            "<code>YOUTUBE_CLIENT_SECRET</code> en Render y vuelve a cargar esta página.</div></div>"
        )
    if not conn:
        return (
            "<div class='conn'><b>YouTube no está conectado</b>"
            "<div class='note' style='margin-top:6px'>Al conectar verás una pantalla de Google "
            "diciendo que la app no está verificada. Es normal — entra en Avanzado y continúa.</div>"
            "<div class='row'>"
            "<form method='post' action='/admin/youtube/connect' style='margin:0'>"
            f"<input type='hidden' name='key' value='{kf}'>"
            "<button class='go' type='submit'>Conectar mi canal</button></form>"
            "</div></div>"
        )
    return (
        "<div class='conn'>"
        f"<b>Conectado: {html.escape(conn.get('channel_title') or 'canal')}</b>"
        "<div class='note' style='margin-top:6px'>Revisamos comentarios nuevos cada 5 minutos.</div>"
        "<div class='row'>"
        "<form method='post' action='/admin/youtube/poll' style='margin:0'>"
        f"<input type='hidden' name='key' value='{kf}'>"
        "<button class='ghost' type='submit'>Revisar ahora</button></form>"
        "<form method='post' action='/admin/youtube/disconnect' style='margin:0'>"
        f"<input type='hidden' name='key' value='{kf}'>"
        "<button class='ghost' type='submit'>Desconectar</button></form>"
        "</div></div>"
    )


def _card(kf: str, c: dict) -> str:
    cid = html.escape(c["comment_id"])
    label, bg, fg = _KIND_LABEL.get(c.get("kind") or "pending", _KIND_LABEL["pending"])
    vid = c.get("video_id") or ""
    title = c.get("video_title") or vid or "—"
    link = f"https://www.youtube.com/watch?v={html.escape(vid)}&lc={cid}" if vid else ""
    head = (
        "<div class='top'>"
        f"<span class='who'>{html.escape(c.get('author') or 'Anónimo')}</span>"
        f"<span class='kind' style='background:{bg};color:{fg}'>{label}</span>"
        + (f"<a class='vid' href='{link}' target='_blank' rel='noopener'>{html.escape(title)}</a>"
           if link else f"<span class='vid'>{html.escape(title)}</span>")
        + f"<span class='when'>{_fmt_time(c['published_at']) if c.get('published_at') else '—'}</span>"
        "</div>"
    )
    body = f"<div class='body'>{html.escape(c.get('body') or '')}</div>"

    if c.get("status") == "replied":
        return (
            "<div class='ytc'>" + head + body
            + "<div class='note' style='margin:0 0 6px'>Respondido "
            + (_fmt_time(c["handled_at"]) if c.get("handled_at") else "") + "</div>"
            + f"<div class='body' style='background:#eef6ff'>{html.escape(c.get('reply_text') or '')}</div>"
            "</div>"
        )

    if c.get("status") in ("skipped", "archived"):
        return "<div class='ytc' style='opacity:.6'>" + head + body + "</div>"

    draft = c.get("draft") or ""
    editor = (
        f"<textarea name='body' maxlength='9000'>{html.escape(draft)}</textarea>"
        if draft else
        "<div class='nodraft'>Sin borrador. Genera uno o descarta el comentario.</div>"
        "<textarea name='body' maxlength='9000' placeholder='Escribe la respuesta…'></textarea>"
    )
    return (
        "<div class='ytc'>" + head + body
        + "<form method='post' action='/admin/youtube/approve' style='margin:0'>"
        + f"<input type='hidden' name='key' value='{kf}'>"
        + f"<input type='hidden' name='comment_id' value='{cid}'>"
        + editor
        + "<div class='acts'>"
        + "<button class='go' type='submit'>Publicar respuesta</button>"
        + f"<button class='sec2' type='submit' formaction='/admin/youtube/regenerate'>Regenerar</button>"
        + f"<button class='warn' type='submit' formaction='/admin/youtube/skip'>Descartar</button>"
        + "</div></form></div>"
    )


def _page(key: str, conn: dict | None, rows: list[dict], counts: dict, tab: str, msg: str, bad: bool) -> str:
    kf = html.escape(key)
    tabs = "".join(
        f"<a class='{'on' if tab == slug else ''}' href='/admin/youtube?key={kf}&tab={slug}'>"
        f"{label}{(' (' + str(counts.get(slug, 0)) + ')') if slug != 'all' else ''}</a>"
        for slug, label in _TABS
    )
    flash = f"<div class='flash{' bad' if bad else ''}'>{html.escape(msg)}</div>" if msg else ""
    cards = "".join(_card(kf, c) for c in rows) or (
        "<div class='empty'>Nada por aquí todavía.</div>"
    )
    body = (
        _css()
        + "<h1>Comentarios de YouTube</h1>"
        + "<div class='sub'>El asistente redacta, tú apruebas. Nada se publica sin tu clic.</div>"
        + flash
        + _connect_box(kf, conn)
        + f"<div class='filters'>{tabs}</div>"
        + cards
    )
    return _admin_page("YouTube — Tropical Glitz", body)


def _auth_ok(key: str) -> bool:
    return bool(_settings.admin_token) and key == _settings.admin_token


def _back(key: str, msg: str = "", bad: bool = False, tab: str = "pending") -> RedirectResponse:
    url = f"/admin/youtube?key={key}&tab={tab}"
    if msg:
        from urllib.parse import quote
        url += f"&msg={quote(msg)}&bad={'1' if bad else '0'}"
    return RedirectResponse(url, status_code=303)


@router.get("/admin/youtube", response_class=HTMLResponse)
async def admin_youtube(
    key: str = "",
    tab: str = "pending",
    msg: str = "",
    bad: str = "0",
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    if not _settings.admin_token:
        return HTMLResponse(_admin_locked("El panel está deshabilitado: falta ADMIN_TOKEN."), 401)
    if not _auth_ok(key):
        return HTMLResponse(_admin_locked("Token inválido."), 401)

    conn = await youtube.connection(session)
    counts = await yt_comments.counters(session)
    rows = await yt_comments.listing(session, status=tab)
    return HTMLResponse(_page(key, conn, rows, counts, tab, msg, bad == "1"))


@router.post("/admin/youtube/connect")
async def yt_connect(request: Request):
    form = await _form_data(request)
    key = form.get("key", "")
    if not _auth_ok(key):
        return HTMLResponse(_admin_locked("Token inválido."), 401)
    if not youtube.configured():
        return _back(key, "Faltan YOUTUBE_CLIENT_ID y YOUTUBE_CLIENT_SECRET en Render.", True)
    nonce = secrets.token_urlsafe(24)
    _pending_states[nonce] = key
    return RedirectResponse(youtube.auth_url(nonce), status_code=303)


@router.get("/admin/youtube/callback", response_class=HTMLResponse)
async def yt_callback(
    code: str = "",
    state: str = "",
    error: str = "",
    session: AsyncSession = Depends(get_session),
):
    key = _pending_states.pop(state, "")
    if not key or not _auth_ok(key):
        return HTMLResponse(_admin_locked("Sesión de conexión no válida. Vuelve a intentarlo."), 401)
    if error:
        return _back(key, f"Google canceló la conexión: {error}", True)
    if not code:
        return _back(key, "Google no devolvió el código de autorización.", True)
    try:
        me = await youtube.exchange_code(session, code)
    except Exception as exc:  # noqa: BLE001
        _log.exception("Falló el intercambio de código de YouTube")
        return _back(key, str(exc)[:300], True)
    return _back(key, f"Canal conectado: {me.get('title') or me.get('id')}")


@router.post("/admin/youtube/disconnect")
async def yt_disconnect(request: Request, session: AsyncSession = Depends(get_session)):
    form = await _form_data(request)
    key = form.get("key", "")
    if not _auth_ok(key):
        return HTMLResponse(_admin_locked("Token inválido."), 401)
    await youtube.disconnect(session)
    return _back(key, "Canal desconectado.")


@router.post("/admin/youtube/poll")
async def yt_poll(request: Request, session: AsyncSession = Depends(get_session)):
    form = await _form_data(request)
    key = form.get("key", "")
    if not _auth_ok(key):
        return HTMLResponse(_admin_locked("Token inválido."), 401)
    try:
        res = await yt_comments.poll_once(session)
    except Exception as exc:  # noqa: BLE001
        _log.exception("Falló el sondeo manual de YouTube")
        return _back(key, str(exc)[:300], True)
    if not res.get("ok"):
        return _back(key, res.get("error", "No se pudo revisar."), True)
    return _back(
        key,
        f"Revisados {res['scanned']} comentarios · {res['nuevos']} nuevos · "
        f"{res.get('borradores', 0)} borradores",
    )


@router.post("/admin/youtube/approve")
async def yt_approve(request: Request, session: AsyncSession = Depends(get_session)):
    form = await _form_data(request)
    key = form.get("key", "")
    if not _auth_ok(key):
        return HTMLResponse(_admin_locked("Token inválido."), 401)
    cid = form.get("comment_id", "")
    body = (form.get("body") or "").strip()
    if not cid:
        return _back(key, "Falta el comentario.", True)
    if not body:
        return _back(key, "La respuesta está vacía.", True)
    try:
        reply_id = await youtube.post_reply(session, cid, body)
    except Exception as exc:  # noqa: BLE001
        _log.exception("Falló publicar la respuesta en YouTube")
        return _back(key, str(exc)[:300], True)
    await yt_comments.mark_replied(session, cid, body, reply_id)
    return _back(key, "Respuesta publicada.")


@router.post("/admin/youtube/skip")
async def yt_skip(request: Request, session: AsyncSession = Depends(get_session)):
    form = await _form_data(request)
    key = form.get("key", "")
    if not _auth_ok(key):
        return HTMLResponse(_admin_locked("Token inválido."), 401)
    cid = form.get("comment_id", "")
    if cid:
        await yt_comments.set_status(session, cid, "skipped")
    return _back(key, "Comentario descartado.")


@router.post("/admin/youtube/regenerate")
async def yt_regenerate(request: Request, session: AsyncSession = Depends(get_session)):
    form = await _form_data(request)
    key = form.get("key", "")
    if not _auth_ok(key):
        return HTMLResponse(_admin_locked("Token inválido."), 401)
    cid = form.get("comment_id", "")
    if not cid:
        return _back(key, "Falta el comentario.", True)
    try:
        await yt_comments.regenerate(session, cid)
    except Exception as exc:  # noqa: BLE001
        _log.exception("Falló regenerar el borrador")
        return _back(key, str(exc)[:300], True)
    return _back(key, "Borrador regenerado.")
