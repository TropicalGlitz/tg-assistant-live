"""Endpoints de chat del widget.

Dos vías:
- POST /chat            → respuesta JSON completa (simple).
- GET  /apps/assistant  → App Proxy de Shopify + streaming SSE (producción).
  El widget llama a `https://<tienda>.myshopify.com/apps/assistant?...&signature=...`
  y Shopify reenvía a este backend firmando la petición (verificada abajo).
"""
from __future__ import annotations

import json
from pathlib import Path

import html

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Response

from app.core.config import get_settings
from app.core.security import verify_app_proxy_signature
from app.db.session import get_session
from app.services import conversations, proactive, rag

router = APIRouter(tags=["chat"])
_settings = get_settings()

# Widget del storefront servido desde el backend: el tema solo carga
# <script src=".../widget.js" defer></script>, así se actualiza sin tocar el tema.
_WIDGET_JS = Path(__file__).resolve().parents[2] / "static" / "widget.js"


@router.get("/widget.js")
async def widget_js() -> FileResponse:
    return FileResponse(
        _WIDGET_JS,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )


_LOGO_PNG = Path(__file__).resolve().parents[2] / "static" / "tg-logo.png"


@router.get("/tg-logo.png")
async def tg_logo() -> FileResponse:
    return FileResponse(
        _LOGO_PNG,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=86400"},
    )


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)
    session_id: str | None = None
    max_price: float | None = None


@router.post("/chat")
async def chat(req: ChatRequest, session: AsyncSession = Depends(get_session)):
    result = await rag.answer_query(session, req.message, max_price=req.max_price)
    # Registra la conversación para supervisión (no debe romper la respuesta).
    await conversations.log_exchange(
        session,
        session_id=req.session_id,
        message=req.message,
        answer=result.get("answer", ""),
        handoff=bool(result.get("handoff")),
        sources=result.get("sources"),
    )
    return result


# ---------------------------------------------------------------------------
# Panel de supervisión: /admin/conversations?key=ADMIN_TOKEN
# Lee (no modifica) las conversaciones para que el dueño vea qué responde el AI.
# ---------------------------------------------------------------------------

_MODE_LABEL = {
    "catalog": ("Catálogo/FAQ", "#12b76a"),
    "general": ("Conocimiento general", "#3b82f6"),
    "handoff": ("Derivó a contacto", "#f59e0b"),
}


def _admin_page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>"
        "<style>"
        "*{box-sizing:border-box}"
        "body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
        "background:#f5f5f7;color:#1b1b1f}"
        ".wrap{max-width:900px;margin:0 auto;padding:20px}"
        "h1{font-size:20px;margin:0 0 4px}.sub{color:#6b6b74;font-size:13px;margin-bottom:16px}"
        ".stats{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px}"
        ".stat{background:#fff;border:1px solid #eee;border-radius:12px;padding:10px 14px;min-width:110px}"
        ".stat b{display:block;font-size:22px}.stat span{font-size:12px;color:#6b6b74}"
        ".filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}"
        ".filters a{font-size:13px;text-decoration:none;border:1px solid #d7d7de;border-radius:999px;"
        "padding:6px 12px;color:#1b1b1f;background:#fff}.filters a.on{background:#ef2c8f;color:#fff;border-color:#ef2c8f}"
        ".sess{background:#fff;border:1px solid #eee;border-radius:14px;padding:12px 14px;margin-bottom:14px}"
        ".sess h3{margin:0 0 8px;font-size:12px;color:#6b6b74;font-weight:600}"
        ".ex{border-top:1px solid #f0f0f2;padding:10px 0}.ex:first-of-type{border-top:0}"
        ".q{font-weight:600;margin:0 0 4px}.a{margin:0;white-space:pre-wrap;color:#2a2a30}"
        ".meta{margin-top:6px;font-size:11px;color:#9a9aa2}"
        ".tag{display:inline-block;color:#fff;border-radius:999px;padding:2px 8px;font-size:11px;margin-right:6px}"
        ".empty{background:#fff;border:1px solid #eee;border-radius:14px;padding:30px;text-align:center;color:#6b6b74}"
        "a.link{color:#ef2c8f}"
        "</style></head><body><div class='wrap'>"
        + body
        + "</div></body></html>"
    )


def _admin_locked(msg: str) -> str:
    body = (
        "<h1>Panel de conversaciones</h1>"
        f"<div class='empty'>{html.escape(msg)}</div>"
    )
    return _admin_page("Panel — Tropical Glitz", body)


def _fmt_time(dt) -> str:
    try:
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:  # noqa: BLE001
        return str(dt)


def _render_admin(rows: list, st: dict, key: str, mode: str) -> str:
    def q(m: str) -> str:
        base = f"?key={html.escape(key)}"
        return base + (f"&mode={m}" if m else "")

    filters = "".join(
        f"<a class='{'on' if mode == m else ''}' href='{q(m)}'>{lbl}</a>"
        for m, lbl in [("", "Todas"), ("general", "Conocimiento general"),
                       ("catalog", "Catálogo/FAQ"), ("handoff", "Derivó a contacto")]
    )
    stat_html = "".join(
        f"<div class='stat'><b>{st.get(k, 0)}</b><span>{lbl}</span></div>"
        for k, lbl in [("total", "Total"), ("last24h", "Últimas 24 h"),
                       ("general", "Conoc. general"), ("catalog", "Catálogo/FAQ"),
                       ("handoff", "A contacto")]
    )

    # Agrupa por sesión, manteniendo el orden (rows viene de más nuevo a más viejo).
    order: list[str] = []
    groups: dict[str, list] = {}
    for r in rows:
        sid = r.get("session_id") or "—"
        if sid not in groups:
            groups[sid] = []
            order.append(sid)
        groups[sid].append(r)

    if not rows:
        sessions_html = "<div class='empty'>Aún no hay conversaciones registradas. Cuando un cliente escriba en el chat, aparecerán aquí.</div>"
    else:
        blocks = []
        for sid in order:
            exs = list(reversed(groups[sid]))  # dentro de la sesión, cronológico
            label = "Visitante " + sid[:8] if sid != "—" else "Sin sesión"
            ex_html = []
            for r in exs:
                lbl, color = _MODE_LABEL.get(r.get("mode") or "general", ("—", "#9a9aa2"))
                ex_html.append(
                    "<div class='ex'>"
                    f"<p class='q'>🧑 {html.escape(r.get('message') or '')}</p>"
                    f"<p class='a'>🌴 {html.escape(r.get('answer') or '')}</p>"
                    f"<div class='meta'><span class='tag' style='background:{color}'>{lbl}</span>"
                    f"{_fmt_time(r.get('created_at'))}</div></div>"
                )
            blocks.append(
                f"<div class='sess'><h3>{html.escape(label)} · {len(exs)} mensaje(s)</h3>"
                + "".join(ex_html)
                + "</div>"
            )
        sessions_html = "".join(blocks)

    body = (
        "<h1>Panel de conversaciones</h1>"
        "<div class='sub'>Tropical Glitz AI · lectura de supervisión (se actualiza al recargar)</div>"
        f"<div class='stats'>{stat_html}</div>"
        f"<div class='filters'>{filters}</div>"
        f"{sessions_html}"
    )
    return _admin_page("Panel — Tropical Glitz", body)


@router.get("/admin/conversations", response_class=HTMLResponse)
async def admin_conversations(
    key: str = "", mode: str = "", session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    if not _settings.admin_token:
        return HTMLResponse(
            _admin_locked("Falta configurar ADMIN_TOKEN en Render (Environment). "
                          "Agrega esa variable con un valor secreto y redepliega."),
            status_code=503,
        )
    if key != _settings.admin_token:
        return HTMLResponse(
            _admin_locked("Token inválido o ausente. Abre esta página con ?key=TU_TOKEN al final de la URL."),
            status_code=401,
        )
    st = await conversations.stats(session)
    rows = await conversations.fetch_recent(session, limit=500, mode=(mode or None))
    return HTMLResponse(_render_admin(rows, st, key, mode))


@router.get("/apps/assistant")
async def chat_proxy(request: Request, session: AsyncSession = Depends(get_session)):
    """Entrada de producción vía Shopify App Proxy, con streaming SSE."""
    params = dict(request.query_params)
    if not verify_app_proxy_signature(params, _settings.shopify_api_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid proxy signature")

    message = (params.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty message")
    max_price = float(params["max_price"]) if params.get("max_price") else None

    async def event_stream():
        async for chunk in rag.answer_query_stream(session, message, max_price=max_price):
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/apps/proactive")
async def proactive_message(request: Request, session: AsyncSession = Depends(get_session)):
    """Decide qué mensaje proactivo disparar según el contexto del navegador (13 triggers)."""
    params = dict(request.query_params)
    if not verify_app_proxy_signature(params, _settings.shopify_api_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid proxy signature")

    ctx = {
        "page_type": params.get("page_type", "other"),
        "signal": params.get("signal", "idle"),
        "is_returning": params.get("is_returning") == "1",
        "has_purchased": params.get("has_purchased") == "1",
        "in_stock": None if "in_stock" not in params else params.get("in_stock") == "1",
        "product_title": params.get("product_title"),
        "collection": params.get("collection"),
        "cart": [s for s in (params.get("cart") or "").split("|") if s],
        "cart_total": float(params["cart_total"]) if params.get("cart_total") else 0.0,
    }
    result = await proactive.resolve(session, ctx)
    if not result:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return result
