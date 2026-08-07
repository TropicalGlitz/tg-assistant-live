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
from app.services import conversations, events, mailer, orders, proactive, rag

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
    # Historial de la sesión = memoria: el asistente continúa la conversación.
    history = await conversations.recent_history(session, req.session_id)
    result = await rag.answer_query(session, req.message, max_price=req.max_price, history=history)
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


class ContactRequest(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=80)
    last_name: str = Field("", max_length=80)
    phone: str = Field("", max_length=40)
    email: str = Field(..., min_length=3, max_length=160)
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str | None = None


@router.post("/contact")
async def contact(req: ContactRequest, session: AsyncSession = Depends(get_session)):
    """El cliente pide hablar con un representante: se envía un correo a soporte
    con sus datos + su pregunta + toda la conversación. Además se registra el lead
    en el panel para no perderlo aunque el email falle."""
    transcript = await conversations.session_transcript(session, req.session_id)

    name = f"{req.first_name} {req.last_name}".strip()
    await conversations.log_exchange(
        session,
        session_id=req.session_id,
        message=f"[Contact request] {name} · {req.email} · {req.phone or 'no phone'}",
        answer=req.message,
        handoff=True,
        sources=None,
    )

    ok, err = await mailer.send_contact_email(
        first_name=req.first_name,
        last_name=req.last_name,
        phone=req.phone,
        email=req.email,
        message=req.message,
        transcript=transcript,
    )
    if not ok:
        code = (
            status.HTTP_503_SERVICE_UNAVAILABLE
            if err == "email_not_configured"
            else status.HTTP_502_BAD_GATEWAY
        )
        raise HTTPException(status_code=code, detail=err or "send_failed")
    return {"ok": True}


class EventRequest(BaseModel):
    session_id: str | None = None
    type: str = Field(..., min_length=1, max_length=40)
    title: str | None = Field(None, max_length=200)
    variant_id: str | None = Field(None, max_length=40)
    price: float | None = None


@router.post("/event")
async def event(req: EventRequest, session: AsyncSession = Depends(get_session)):
    """Registra un evento ligero del widget (p. ej. `add_to_cart`) para el panel de
    control. Solo aceptamos tipos de una lista blanca; nunca rompe el chat."""
    if req.type not in events.ALLOWED_TYPES:
        return {"ok": False, "ignored": True}
    await events.log_event(
        session,
        session_id=req.session_id,
        type=req.type,
        title=req.title,
        variant_id=req.variant_id,
        price=req.price,
    )
    return {"ok": True}


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
        ".wrap{max-width:1040px;margin:0 auto;padding:20px}"
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
        ".kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:18px}"
        ".kpi{background:#fff;border:1px solid #eee;border-radius:14px;padding:14px 16px}"
        ".kpi b{display:block;font-size:26px;line-height:1.1}.kpi span{font-size:12px;color:#6b6b74}"
        ".kpi.money{background:linear-gradient(135deg,#ef2c8f,#b81c6f);border:0;color:#fff}"
        ".kpi.money span{color:#ffd9ec}"
        ".kpi.good b{color:#12b76a}.kpi.warn b{color:#f59e0b}"
        ".sec{background:#fff;border:1px solid #eee;border-radius:16px;padding:16px 18px;margin-bottom:18px}"
        ".sec h2{font-size:15px;margin:0 0 12px}"
        ".funnel{display:flex;flex-direction:column;gap:8px}"
        ".frow{display:flex;align-items:center;gap:10px;font-size:13px}"
        ".frow .lbl{width:150px;color:#4a4a52;flex:none}"
        ".fbar{height:22px;border-radius:6px;background:#ef2c8f;min-width:2px}"
        ".fbar.b2{background:#a855f7}.fbar.b3{background:#12b76a}"
        ".frow .val{font-weight:700}.frow .pct{color:#9a9aa2;font-size:12px}"
        "table.sales{width:100%;border-collapse:collapse;font-size:13px}"
        "table.sales th,table.sales td{text-align:left;padding:8px 10px;border-bottom:1px solid #f0f0f2}"
        "table.sales th{color:#6b6b74;font-weight:600;font-size:12px}"
        "table.sales td.n{text-align:right;font-variant-numeric:tabular-nums}"
        "table.sales tfoot td{font-weight:800;border-top:2px solid #eee}"
        ".date-h{font-size:12px;font-weight:700;color:#9a9aa2;margin:18px 0 8px;text-transform:uppercase;letter-spacing:.03em}"
        ".badge{display:inline-block;border-radius:999px;padding:2px 9px;font-size:11px;font-weight:600;margin-right:6px}"
        ".badge.sale{background:#dcfce7;color:#166534}.badge.cart{background:#ede9fe;color:#6d28d9}"
        ".badge.human{background:#fef3c7;color:#92400e}.badge.ai{background:#dbeafe;color:#1e40af}"
        ".note{font-size:12px;color:#9a9aa2;margin-top:8px}"
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


def _money(n) -> str:
    try:
        return "$" + format(float(n or 0), ",.2f")
    except Exception:  # noqa: BLE001
        return "$0.00"


def _day_key(dt) -> str:
    try:
        return dt.date().isoformat()
    except Exception:  # noqa: BLE001
        return str(dt)[:10]


def _pct(a: int, b: int) -> str:
    return f"{(100.0 * a / b):.0f}%" if b else "0%"


def _fbar(a: int, b: int) -> int:
    """Ancho de barra en px (0-560) proporcional al máximo del embudo."""
    return int(560 * a / b) if b else 2


def _render_dashboard(rows, st, ev, atc_sessions, ai, key, f) -> str:
    ai_orders = [o for o in ai.get("orders", []) if not o.get("cancelled")]
    purchased_sessions = {o["session_id"] for o in ai_orders if o.get("session_id")}
    session_rev: dict[str, float] = {}
    for o in ai_orders:
        sid = o.get("session_id")
        if sid:
            session_rev[sid] = session_rev.get(sid, 0.0) + float(o.get("ai_revenue") or 0)

    total_sessions = int(st.get("sessions", 0))
    handoff_sessions = int(st.get("handoff_sessions", 0))
    resolved_sessions = max(total_sessions - handoff_sessions, 0)
    atc_count = len(atc_sessions)
    purchased_count = len(purchased_sessions)
    ai_revenue = sum(float(o.get("ai_revenue") or 0) for o in ai_orders)

    # --- KPIs ---
    kpis = [
        ("kpi", str(st.get("total", 0)), "Conversaciones"),
        ("kpi", str(total_sessions), "Clientes únicos"),
        ("kpi", str(st.get("last24h", 0)), "Últimas 24 h"),
        ("kpi good", str(resolved_sessions), "Resueltas por AI"),
        ("kpi warn", str(handoff_sessions), "Pidió humano / email"),
        ("kpi", str(atc_count), "Agregaron al carrito"),
        ("kpi", str(len(ai_orders)), "Ventas del AI"),
        ("kpi money", _money(ai_revenue), "Ingresos del AI"),
    ]
    kpi_html = "".join(
        f"<div class='{cls}'><b>{html.escape(val)}</b><span>{lbl}</span></div>"
        for cls, val, lbl in kpis
    )

    # --- Embudo ---
    funnel = [
        ("Conversaron", total_sessions, "", ""),
        ("Agregaron al carrito", atc_count, "b2", _pct(atc_count, total_sessions) + " del total"),
        ("Compraron (AI)", purchased_count, "b3", _pct(purchased_count, atc_count) + " del carrito"),
    ]
    funnel_html = "".join(
        f"<div class='frow'><span class='lbl'>{lbl}</span>"
        f"<span class='fbar {cls}' style='width:{_fbar(v, total_sessions)}px'></span>"
        f"<span class='val'>{v}</span> <span class='pct'>{note}</span></div>"
        for lbl, v, cls, note in funnel
    )

    # --- Ventas atribuidas al AI ---
    if ai.get("error"):
        sales_html = "<div class='note'>No se pudieron leer las órdenes de Shopify en este momento (se reintenta al recargar).</div>"
    elif not ai_orders:
        sales_html = "<div class='note'>Aún no hay ventas atribuidas al AI. Aparecerán aquí cuando un cliente compre un producto que agregó al carrito desde el chat.</div>"
    else:
        trs = ""
        for o in ai_orders[:100]:
            sid = (o.get("session_id") or "")[:8]
            trs += (
                "<tr>"
                f"<td>{html.escape(str(o.get('order') or ''))}</td>"
                f"<td>{html.escape(str(o.get('created_at') or ''))}</td>"
                f"<td>{html.escape('Visitante ' + sid if sid else '—')}</td>"
                f"<td>{html.escape(str(o.get('financial_status') or ''))}</td>"
                f"<td class='n'>{_money(o.get('ai_revenue'))}</td>"
                f"<td class='n'>{_money(o.get('total'))}</td>"
                "</tr>"
            )
        total_all = sum(float(o.get("total") or 0) for o in ai_orders)
        trunc = "<div class='note'>Mostrando las órdenes más recientes; hay más historial disponible.</div>" if ai.get("truncated") else ""
        sales_html = (
            "<table class='sales'><thead><tr>"
            "<th>Orden</th><th>Fecha</th><th>Cliente</th><th>Pago</th>"
            "<th class='n'>Venta del AI</th><th class='n'>Total orden</th>"
            "</tr></thead><tbody>" + trs + "</tbody>"
            "<tfoot><tr><td colspan='4'>Total</td>"
            f"<td class='n'>{_money(ai_revenue)}</td><td class='n'>{_money(total_all)}</td></tr></tfoot>"
            "</table>" + trunc
        )

    # --- Filtros ---
    def q(val: str) -> str:
        base = f"?key={html.escape(key)}"
        return base + (f"&f={val}" if val else "")

    filters = "".join(
        f"<a class='{'on' if f == val else ''}' href='{q(val)}'>{lbl}</a>"
        for val, lbl in [
            ("", "Todas"), ("resolved", "Resueltas por AI"),
            ("human", "Pidió humano / email"), ("cart", "Agregaron al carrito"),
            ("sale", "Con venta"),
        ]
    )

    # --- Conversaciones agrupadas por cliente y por fecha ---
    order: list[str] = []
    groups: dict[str, list] = {}
    for r in rows:
        sid = r.get("session_id") or "—"
        if sid not in groups:
            groups[sid] = []
            order.append(sid)
        groups[sid].append(r)

    sessions_html = ""
    current_day = None
    shown = 0
    for sid in order:
        exs_desc = groups[sid]
        has_handoff = any(r.get("handoff") for r in exs_desc)
        in_cart = sid in atc_sessions
        sold = sid in purchased_sessions
        # Aplica el filtro seleccionado.
        if f == "resolved" and has_handoff:
            continue
        if f == "human" and not has_handoff:
            continue
        if f == "cart" and not in_cart:
            continue
        if f == "sale" and not sold:
            continue
        shown += 1
        day = _day_key(exs_desc[0].get("created_at"))
        if day != current_day:
            current_day = day
            sessions_html += f"<div class='date-h'>{html.escape(day)}</div>"

        exs = list(reversed(exs_desc))  # cronológico dentro de la sesión
        label = "Visitante " + sid[:8] if sid != "—" else "Sin sesión"
        badges = ""
        if sold:
            badges += f"<span class='badge sale'>💳 {_money(session_rev.get(sid))}</span>"
        if in_cart:
            badges += "<span class='badge cart'>🛒 agregó al carrito</span>"
        badges += (
            "<span class='badge human'>🙋 pidió humano</span>" if has_handoff
            else "<span class='badge ai'>✅ resuelta por AI</span>"
        )
        ex_html = ""
        for r in exs:
            lbl, color = _MODE_LABEL.get(r.get("mode") or "general", ("—", "#9a9aa2"))
            ex_html += (
                "<div class='ex'>"
                f"<p class='q'>🧑 {html.escape(r.get('message') or '')}</p>"
                f"<p class='a'>🌴 {html.escape(r.get('answer') or '')}</p>"
                f"<div class='meta'><span class='tag' style='background:{color}'>{lbl}</span>"
                f"{_fmt_time(r.get('created_at'))}</div></div>"
            )
        sessions_html += (
            f"<div class='sess'><h3>{html.escape(label)} · {len(exs)} mensaje(s)</h3>"
            f"<div style='margin:0 0 8px'>{badges}</div>" + ex_html + "</div>"
        )

    if not shown:
        sessions_html = "<div class='empty'>No hay conversaciones para este filtro.</div>"

    body = (
        "<h1>Panel de control · Tropical Glitz AI</h1>"
        "<div class='sub'>Métricas y conversaciones del asistente (se actualiza al recargar)</div>"
        f"<div class='kpis'>{kpi_html}</div>"
        f"<div class='sec'><h2>Embudo de conversión</h2><div class='funnel'>{funnel_html}</div></div>"
        f"<div class='sec'><h2>Ventas generadas por el AI</h2>{sales_html}</div>"
        "<h2 style='font-size:16px;margin:0 0 10px'>Conversaciones</h2>"
        f"<div class='filters'>{filters}</div>"
        f"{sessions_html}"
    )
    return _admin_page("Panel — Tropical Glitz", body)


@router.get("/admin/conversations", response_class=HTMLResponse)
async def admin_conversations(
    key: str = "", f: str = "", mode: str = "", session: AsyncSession = Depends(get_session)
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
    rows = await conversations.fetch_recent(session, limit=1000, mode=None)
    ev = await events.stats(session)
    atc_sessions = await events.sessions_with_add_to_cart(session)
    try:
        ai = await orders.ai_attributed_orders(days=90)
    except Exception:  # noqa: BLE001
        ai = {"orders": [], "truncated": False, "error": True}
    return HTMLResponse(_render_dashboard(rows, st, ev, atc_sessions, ai, key, (f or "")))


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
