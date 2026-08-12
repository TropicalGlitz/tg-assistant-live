"""Endpoints de chat del widget.

Dos vías:
- POST /chat            → respuesta JSON completa (simple).
- GET  /apps/assistant  → App Proxy de Shopify + streaming SSE (producción).
  El widget llama a `https://<tienda>.myshopify.com/apps/assistant?...&signature=...`
  y Shopify reenvía a este backend firmando la petición (verificada abajo).
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import math
from pathlib import Path

import html

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)
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


@router.get("/suggest")
async def suggest(page_type: str = "", title: str = "", collection: str = ""):
    """Saludo y preguntas sugeridas para la página que el cliente está viendo.

    El widget lo llama al abrir el chat con el tipo de página y el título del
    producto. Son reglas (no LLM): respuesta instantánea, sin costo por token, y
    se ajustan aquí sin tocar el tema de Shopify. Público: no devuelve nada que
    no esté ya en la página que el cliente tiene enfrente."""
    from app.services import suggestions

    return suggestions.suggest(
        page_type=(page_type or "")[:30],
        product_title=(title or "")[:200],
        collection=(collection or "")[:120],
    )


@router.get("/history")
async def history(session_id: str = "", session: AsyncSession = Depends(get_session)):
    """Historial de la sesión para que el widget REPINTE la conversación cuando el
    cliente navega (p. ej. hace click en un producto que le recomendó el AI y
    luego vuelve). El session_id es el que el propio navegador guarda; se
    devuelven solo pregunta/respuesta y nunca las filas del formulario de
    contacto (llevan email y teléfono)."""
    sid = (session_id or "").strip()
    if not sid or len(sid) > 64:
        return {"messages": []}
    msgs = await conversations.session_history_public(session, sid, limit=20)
    return {"messages": msgs}


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


@router.get("/admin/ingest-kb")
async def ingest_kb(key: str = ""):
    """(Re)ingiere las páginas de políticas/FAQ del sitio a la base de conocimiento.
    Úsalo tras actualizar una política para que el AI responda con lo más reciente."""
    if not _settings.admin_token or key != _settings.admin_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    from app.services import policy_ingest

    try:
        result = await policy_ingest.run()
        return {"ok": True, "ingested": result}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e)[:300])


@router.get("/admin/ingest-videos")
async def ingest_videos(key: str = ""):
    """(Re)ingiere el catálogo de videos del canal de YouTube en segundo plano.
    Responde de inmediato; el trabajo (fetch + embeddings de lo nuevo) corre detrás
    para no chocar con el timeout del proxy. Revisa el conteo en /admin o en la BD."""
    if not _settings.admin_token or key != _settings.admin_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    from app.services import video_ingest

    _spawn_bg(video_ingest.run_startup())
    return {"ok": True, "started": True}


class VideoUploadItem(BaseModel):
    id: str = Field(..., min_length=6, max_length=20)
    title: str = Field(..., min_length=1, max_length=300)


class VideoUpload(BaseModel):
    videos: list[VideoUploadItem]


_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")

# Referencias fuertes a tareas de fondo: sin esto, asyncio puede recolectarlas
# a mitad de ejecución (garbage collection) y mueren en silencio.
_BG_TASKS: set = set()


def _spawn_bg(coro) -> None:
    import asyncio

    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


@router.post("/admin/upload-videos")
async def upload_videos(payload: VideoUpload, key: str = ""):
    """Backfill único del catálogo de videos: recibe [(id, título)] extraídos del
    canal y los ingiere en segundo plano (idempotente por hash). Complementa la
    ingesta automática de arranque, que solo alcanza las primeras páginas."""
    if not _settings.admin_token or key != _settings.admin_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    if len(payload.videos) > 3000:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="too many")
    vids = [
        (v.id, v.title.strip())
        for v in payload.videos
        if _VIDEO_ID_RE.match(v.id) and v.title.strip()
    ]
    from app.services import video_ingest

    _spawn_bg(video_ingest.ingest_list_safe(vids))
    return {"ok": True, "received": len(vids), "started": True}


# ---------------------------------------------------------------------------
# Transcripciones de videos: el navegador (con sesión de YouTube) extrae la URL
# firmada de subtítulos por video y la manda aquí; el servidor descarga el texto
# DESDE YOUTUBE (la URL se valida contra el video y el catálogo, así que el
# contenido no lo controla quien llama) y lo embebe como conocimiento.
# ---------------------------------------------------------------------------


@router.get("/admin/transcript-status")
async def transcript_status():
    """Estado de la ingesta de transcripciones (solo lectura, datos públicos del
    canal): cuántos videos hay, cuántos ya tienen transcripción y cuáles faltan."""
    from app.services import video_ingest

    videos = await video_ingest.catalog_videos()
    done = await video_ingest.transcribed_ids()
    missing = [
        {"id": vid, "title": title} for vid, title in videos if vid not in done
    ]
    return {
        "videos": len(videos),
        "transcribed": len(done),
        "missing": len(missing),
        "pending": missing[:3000],
    }


class TranscriptItem(BaseModel):
    id: str = Field(..., min_length=6, max_length=20)
    title: str = Field(..., min_length=1, max_length=300)
    url: str = Field(..., min_length=30, max_length=4000)
    # Texto de respaldo (descargado por el navegador del cliente): se usa SOLO
    # si la descarga directa desde YouTube falla (rate limit de IP datacenter).
    text: str = Field("", max_length=200000)


class TranscriptUpload(BaseModel):
    items: list[TranscriptItem]


@router.post("/admin/upload-transcripts")
async def upload_transcripts(payload: TranscriptUpload):
    """Recibe [(id, título, URL firmada de subtítulos)] y arranca la ingesta en
    segundo plano. Seguridad sin token: solo se aceptan URLs https de
    youtube.com/api/timedtext firmadas para ESE video, y solo videos que ya están
    en el catálogo del canal — el texto siempre viene de YouTube, nunca del caller."""
    from app.services import video_ingest

    if len(payload.items) > 300:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="too many")
    catalog = {vid for vid, _ in await video_ingest.catalog_videos()}
    items = [
        (it.id, " ".join(it.title.split()), it.url, it.text)
        for it in payload.items
        if _VIDEO_ID_RE.match(it.id)
        and it.id in catalog
        and video_ingest.valid_timedtext_url(it.url, it.id)
    ]
    _spawn_bg(video_ingest.ingest_transcripts_safe(items))
    return {"ok": True, "received": len(payload.items), "accepted": len(items), "started": True}


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
        ".daterow{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:16px}"
        ".daterow form{display:flex;gap:6px;align-items:center;margin:0}"
        ".daterow input[type=date]{border:1px solid #d7d7de;border-radius:8px;padding:6px 8px;font-size:13px}"
        ".daterow .apply{border:0;background:#ef2c8f;color:#fff;border-radius:8px;padding:7px 12px;font-size:13px;font-weight:600;cursor:pointer}"
        ".presets{display:flex;gap:6px}"
        ".presets a{font-size:12px;text-decoration:none;border:1px solid #d7d7de;border-radius:999px;padding:6px 12px;color:#1b1b1f;background:#fff}"
        ".presets a.on{background:#1b1b1f;color:#fff;border-color:#1b1b1f}"
        ".rangelbl{font-size:12px;color:#6b6b74;margin-left:auto}"
        ".kpi .delta{font-size:11px;margin-top:3px}.kpi .delta.up{color:#12b76a}.kpi .delta.down{color:#e11d48}"
        ".kpi.money .delta.up,.kpi.money .delta.down{color:#ffd9ec}"
        ".charts{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-bottom:18px}"
        "@media(max-width:720px){.charts{grid-template-columns:1fr}}"
        ".donut{display:flex;align-items:center;gap:14px;flex-wrap:wrap}"
        ".legend{display:flex;flex-direction:column;gap:6px}"
        ".lg{font-size:12px;color:#4a4a52;display:flex;align-items:center;gap:6px}"
        ".dot{width:10px;height:10px;border-radius:3px;display:inline-block}"
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


_UTC = _dt.timezone.utc
_PRESETS = {"7d": 7, "30d": 30, "90d": 90, "all": 3650}


def _resolve_range(frm: str, to: str, rng: str):
    """Devuelve (since, until, prev_since, prev_until, key) a partir de los
    parámetros del calendario (from/to) o de un preset (7d/30d/90d/all)."""
    today = _dt.datetime.now(_UTC).date()
    since = until = None
    key = rng if rng in _PRESETS else ""
    if frm and to:
        try:
            d1 = _dt.date.fromisoformat(frm)
            d2 = _dt.date.fromisoformat(to)
            if d2 < d1:
                d1, d2 = d2, d1
            since = _dt.datetime.combine(d1, _dt.time.min, tzinfo=_UTC)
            until = _dt.datetime.combine(d2, _dt.time.min, tzinfo=_UTC) + _dt.timedelta(days=1)
            key = "custom"
        except ValueError:
            since = until = None
    if since is None:
        n = _PRESETS.get(rng or "30d", 30)
        key = rng if rng in _PRESETS else "30d"
        until = _dt.datetime.combine(today, _dt.time.min, tzinfo=_UTC) + _dt.timedelta(days=1)
        since = until - _dt.timedelta(days=n)
    period = until - since
    return since, until, since - period, since, key


def _delta_html(cur: float, prev: float) -> str:
    """Pequeño indicador de cambio vs. período anterior."""
    if prev <= 0:
        if cur > 0:
            return "<div class='delta up'>▲ nuevo</div>"
        return "<div class='delta' style='color:#b8b8c2'>—</div>"
    pct = (cur - prev) / prev * 100.0
    if abs(pct) < 0.05:
        return "<div class='delta' style='color:#b8b8c2'>0%</div>"
    up = pct > 0
    arrow = "▲" if up else "▼"
    return f"<div class='delta {'up' if up else 'down'}'>{arrow} {abs(pct):.0f}% vs. anterior</div>"


def _bar_chart(points: list, color: str = "#ef2c8f", height: int = 150) -> str:
    """Gráfico de barras en SVG (autocontenido). points: lista de (etiqueta, valor)."""
    points = [(str(lbl), int(v or 0)) for lbl, v in points]
    if not points:
        return "<div class='note'>Sin datos en este rango.</div>"
    maxv = max((v for _, v in points), default=0) or 1
    W = 680
    pad_b, pad_t = 22, 12
    n = len(points)
    slot = W / n
    bw = max(slot * 0.62, 1.0)
    bars = ""
    for i, (lbl, v) in enumerate(points):
        bh = (v / maxv) * (height - pad_b - pad_t)
        x = i * slot + (slot - bw) / 2
        y = height - pad_b - bh
        bars += (
            f"<rect x='{x:.1f}' y='{y:.1f}' width='{bw:.1f}' height='{bh:.1f}' rx='2' "
            f"fill='{color}'><title>{html.escape(lbl)}: {v}</title></rect>"
        )
    first, last = points[0][0], points[-1][0]
    axis = (
        f"<text x='0' y='{height - 6}' font-size='10' fill='#9a9aa2'>{html.escape(first)}</text>"
        f"<text x='{W}' y='{height - 6}' font-size='10' fill='#9a9aa2' text-anchor='end'>{html.escape(last)}</text>"
        f"<text x='0' y='10' font-size='10' fill='#9a9aa2'>máx {maxv}</text>"
    )
    return (
        f"<svg viewBox='0 0 {W} {height}' style='width:100%;height:auto' "
        f"role='img'>{bars}{axis}</svg>"
    )


def _donut(parts: list, size: int = 150) -> str:
    """Dona en SVG. parts: lista de (etiqueta, valor, color)."""
    parts = [(lbl, int(v or 0), col) for lbl, v, col in parts]
    total = sum(v for _, v, _ in parts)
    r = 52.0
    c = size / 2.0
    circ = 2 * math.pi * r
    segs = ""
    off = 0.0
    denom = total or 1
    for lbl, v, col in parts:
        seg = (v / denom) * circ
        segs += (
            f"<circle cx='{c}' cy='{c}' r='{r}' fill='none' stroke='{col}' stroke-width='20' "
            f"stroke-dasharray='{seg:.2f} {circ - seg:.2f}' stroke-dashoffset='{-off:.2f}' "
            f"transform='rotate(-90 {c} {c})'><title>{html.escape(lbl)}: {v}</title></circle>"
        )
        off += seg
    center = (
        f"<text x='{c}' y='{c}' font-size='20' font-weight='700' text-anchor='middle' "
        f"dominant-baseline='central'>{total}</text>"
    )
    svg = f"<svg viewBox='0 0 {size} {size}' width='{size}' height='{size}'>{segs}{center}</svg>"
    legend = "".join(
        f"<div class='lg'><span class='dot' style='background:{col}'></span>{html.escape(lbl)} · {v}</div>"
        for lbl, v, col in parts
    )
    return f"<div class='donut'>{svg}<div class='legend'>{legend}</div></div>"


def _render_dashboard(
    rows, cur, prev, atc_cur, atc_prev_count, ai, prev_ai,
    daily_conv, daily_atc, key, f, since, until, range_key,
) -> str:
    ai_orders = [o for o in ai.get("orders", []) if not o.get("cancelled")]
    purchased_sessions = {o["session_id"] for o in ai_orders if o.get("session_id")}
    session_rev: dict[str, float] = {}
    for o in ai_orders:
        sid = o.get("session_id")
        if sid:
            session_rev[sid] = session_rev.get(sid, 0.0) + float(o.get("ai_revenue") or 0)

    total_sessions = int(cur.get("sessions", 0))
    handoff_sessions = int(cur.get("handoff_sessions", 0))
    resolved_sessions = max(total_sessions - handoff_sessions, 0)
    prev_resolved = max(int(prev.get("sessions", 0)) - int(prev.get("handoff_sessions", 0)), 0)
    atc_count = len(atc_cur)
    purchased_count = len(purchased_sessions)
    ai_revenue = sum(float(o.get("ai_revenue") or 0) for o in ai_orders)

    from_val = since.date().isoformat()
    to_val = (until - _dt.timedelta(days=1)).date().isoformat()

    # --- Rango de fechas: presets + calendario ---
    def _range_qs() -> str:
        if range_key == "custom":
            return f"&from={from_val}&to={to_val}"
        return f"&range={html.escape(range_key)}"

    kf = html.escape(key)
    presets = "".join(
        f"<a class='{'on' if range_key == pk else ''}' "
        f"href='?key={kf}&range={pk}{('&f=' + html.escape(f)) if f else ''}'>{lbl}</a>"
        for pk, lbl in [("7d", "7 días"), ("30d", "30 días"), ("90d", "90 días"), ("all", "Todo")]
    )
    fhidden = f"<input type='hidden' name='f' value='{html.escape(f)}'>" if f else ""
    date_control = (
        "<div class='daterow'>"
        f"<div class='presets'>{presets}</div>"
        "<form method='get'>"
        f"<input type='hidden' name='key' value='{kf}'>{fhidden}"
        f"<input type='date' name='from' value='{from_val}'>"
        f"<input type='date' name='to' value='{to_val}'>"
        "<button class='apply' type='submit'>Aplicar</button>"
        "</form>"
        f"<span class='rangelbl'>{from_val} → {to_val}</span>"
        "</div>"
    )

    # --- KPIs con comparación vs. período anterior ---
    kpis = [
        ("kpi", str(cur.get("total", 0)), "Conversaciones", _delta_html(cur.get("total", 0), prev.get("total", 0))),
        ("kpi", str(total_sessions), "Clientes únicos", _delta_html(total_sessions, prev.get("sessions", 0))),
        ("kpi good", str(resolved_sessions), "Resueltas por AI", _delta_html(resolved_sessions, prev_resolved)),
        ("kpi warn", str(handoff_sessions), "Pidió humano / email", _delta_html(handoff_sessions, prev.get("handoff_sessions", 0))),
        ("kpi", str(atc_count), "Agregaron al carrito", _delta_html(atc_count, atc_prev_count)),
        ("kpi", str(len(ai_orders)), "Ventas del AI", _delta_html(len(ai_orders), prev_ai.get("count", 0))),
        ("kpi money", _money(ai_revenue), "Ingresos del AI", _delta_html(ai_revenue, prev_ai.get("revenue", 0))),
    ]
    kpi_html = "".join(
        f"<div class='{cls}'><b>{html.escape(val)}</b><span>{lbl}</span>{delta}</div>"
        for cls, val, lbl, delta in kpis
    )

    # --- Gráficos (SVG autocontenido) ---
    def _fill(series, value_key):
        m = {d["day"]: int(d.get(value_key, 0) or 0) for d in series}
        start = since.date()
        end = (until - _dt.timedelta(days=1)).date()
        span = (end - start).days + 1
        if 0 < span <= 92:
            out = []
            day = start
            while day <= end:
                out.append((day.isoformat()[5:], m.get(day, 0)))
                day += _dt.timedelta(days=1)
            return out
        # Rango grande: usar solo días con actividad (últimos 92).
        pts = [(str(d["day"])[5:], int(d.get(value_key, 0) or 0)) for d in series]
        return pts[-92:]

    conv_points = _fill(daily_conv, "total")
    conv_chart = _bar_chart(conv_points, color="#ef2c8f")
    donut = _donut([
        ("Resueltas por AI", resolved_sessions, "#12b76a"),
        ("Pidió humano", handoff_sessions, "#f59e0b"),
    ])
    atc_points = _fill(daily_atc, "total")
    atc_total = sum(v for _, v in atc_points)
    atc_chart = (
        _bar_chart(atc_points, color="#a855f7")
        if atc_total > 0
        else "<div class='note'>Aún no hay datos de carrito en este rango.</div>"
    )
    charts_html = (
        "<div class='charts'>"
        "<div class='sec'><h2>Conversaciones por día</h2>" + conv_chart + "</div>"
        "<div class='sec'><h2>Resueltas por AI vs. humano</h2>" + donut + "</div>"
        "</div>"
        "<div class='sec'><h2>Agregados al carrito por día</h2>" + atc_chart + "</div>"
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
        base = f"?key={kf}{_range_qs()}"
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
        in_cart = sid in atc_cur
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
        f"<div class='note' style='margin:-8px 0 14px'>🏷️ <a class='link' href='/admin/promos?key={kf}'>"
        "Administrar códigos de promoción</a></div>"
        f"{date_control}"
        f"<div class='kpis'>{kpi_html}</div>"
        f"{charts_html}"
        f"<div class='sec'><h2>Embudo de conversión</h2><div class='funnel'>{funnel_html}</div></div>"
        f"<div class='sec'><h2>Ventas generadas por el AI</h2>{sales_html}</div>"
        "<h2 style='font-size:16px;margin:0 0 10px'>Conversaciones</h2>"
        f"<div class='filters'>{filters}</div>"
        f"{sessions_html}"
    )
    return _admin_page("Panel — Tropical Glitz", body)


@router.get("/admin/conversations", response_class=HTMLResponse)
async def admin_conversations(
    key: str = "",
    f: str = "",
    rng: str = Query("30d", alias="range"),
    frm: str = Query("", alias="from"),
    to: str = "",
    session: AsyncSession = Depends(get_session),
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
    since, until, prev_since, prev_until, range_key = _resolve_range(frm, to, rng)
    cur = await conversations.stats(session, since=since, until=until)
    prev = await conversations.stats(session, since=prev_since, until=prev_until)
    rows = await conversations.fetch_recent(session, limit=1000, mode=None, since=since, until=until)
    atc_cur = await events.sessions_with_add_to_cart(session, since=since, until=until)
    atc_prev = await events.sessions_with_add_to_cart(session, since=prev_since, until=prev_until)
    daily_conv = await conversations.daily_series(session, since, until)
    daily_atc = await events.daily_add_to_cart(session, since, until)
    try:
        ai_all = await orders.ai_attributed_orders(since=prev_since, until=until)
    except Exception:  # noqa: BLE001
        ai_all = {"orders": [], "truncated": False, "error": True}

    since_day = since.date().isoformat()
    all_orders = ai_all.get("orders", [])
    cur_orders = [o for o in all_orders if str(o.get("created_at", "")) >= since_day]
    prev_orders = [o for o in all_orders if str(o.get("created_at", "")) < since_day]
    ai_cur = {
        "orders": cur_orders,
        "truncated": ai_all.get("truncated", False),
        "error": ai_all.get("error", False),
    }
    prev_ai = {
        "count": len([o for o in prev_orders if not o.get("cancelled")]),
        "revenue": sum(float(o.get("ai_revenue") or 0) for o in prev_orders if not o.get("cancelled")),
    }
    return HTMLResponse(
        _render_dashboard(
            rows, cur, prev, atc_cur, len(atc_prev), ai_cur, prev_ai,
            daily_conv, daily_atc, key, (f or ""), since, until, range_key,
        )
    )


# ---------------------------------------------------------------------------
# Panel de códigos de promoción: /admin/promos?key=ADMIN_TOKEN
# El AI responde que NO hay promoción salvo que aquí haya un código activo.
# ---------------------------------------------------------------------------


async def _form_data(request: Request) -> dict[str, str]:
    """Lee un POST de formulario (application/x-www-form-urlencoded) sin depender
    de python-multipart: los formularios del panel son simples campos de texto."""
    from urllib.parse import parse_qs

    raw = (await request.body()).decode("utf-8", "replace")
    return {k: (v[0] if v else "") for k, v in parse_qs(raw, keep_blank_values=True).items()}


def _promos_page(key: str, rows: list, msg: str = "") -> str:
    kf = html.escape(key)
    active_rows = ""
    past_rows = ""
    for r in rows:
        code = (r.get("code") or "").strip()
        desc = html.escape(r.get("description") or r.get("title") or "")
        when = _fmt_time(r.get("created_at"))
        if r.get("active") and code:
            active_rows += (
                "<tr>"
                f"<td><b style='font-size:15px;letter-spacing:.04em'>{html.escape(code)}</b></td>"
                f"<td>{desc}</td>"
                f"<td class='n' style='color:#9a9aa2;font-size:12px'>{when}</td>"
                "<td class='n'>"
                f"<form method='post' action='/admin/promos/delete' style='margin:0'>"
                f"<input type='hidden' name='key' value='{kf}'>"
                f"<input type='hidden' name='promo_id' value='{r['id']}'>"
                "<button class='del' type='submit'>Desactivar</button>"
                "</form></td></tr>"
            )
        else:
            past_rows += (
                f"<tr><td style='color:#9a9aa2'>{html.escape(code) or '—'}</td>"
                f"<td style='color:#9a9aa2'>{desc}</td>"
                f"<td class='n' style='color:#b8b8c2;font-size:12px'>{when}</td>"
                "<td></td></tr>"
            )

    if active_rows:
        state = (
            "<div class='state on'>✅ <b>Hay un código activo.</b> El asistente se lo dará a los "
            "clientes que pregunten por descuentos y lo mencionará al recomendar productos.</div>"
        )
        table = (
            "<table class='sales'><thead><tr><th>Código</th><th>Descripción</th>"
            "<th class='n'>Creado</th><th class='n'></th></tr></thead>"
            f"<tbody>{active_rows}</tbody></table>"
        )
    else:
        state = (
            "<div class='state off'>⛔ <b>No hay ningún código activo.</b> El asistente le responde "
            "a todo el que pregunte que no hay código de promoción disponible en este momento.</div>"
        )
        table = ""

    past = (
        "<div class='sec'><h2>Códigos anteriores</h2>"
        "<table class='sales'><thead><tr><th>Código</th><th>Descripción</th>"
        f"<th class='n'>Creado</th><th></th></tr></thead><tbody>{past_rows}</tbody></table></div>"
        if past_rows else ""
    )

    flash = f"<div class='flash'>{html.escape(msg)}</div>" if msg else ""

    body = (
        "<h1>Códigos de promoción</h1>"
        "<div class='sub'>Lo que pongas aquí es lo único que el asistente puede decirle a un "
        "cliente sobre descuentos. Si está vacío, responde que no hay promoción.</div>"
        f"{flash}{state}"
        f"<div class='sec'><h2>Agregar un código</h2>"
        "<form method='post' action='/admin/promos/add' class='promoform'>"
        f"<input type='hidden' name='key' value='{kf}'>"
        "<label>Código<input name='code' placeholder='SUMMER20' maxlength='40' required></label>"
        "<label>Descripción<input name='description' placeholder='20% de descuento en toda la tienda' maxlength='200'></label>"
        "<button class='apply' type='submit'>Activar código</button>"
        "</form>"
        "<div class='note'>El código queda activo hasta que le des <b>Desactivar</b>. "
        "El asistente lo escribe tal cual lo pongas aquí.</div></div>"
        + (f"<div class='sec'><h2>Código activo</h2>{table}</div>" if table else "")
        + past
        + f"<div class='note'><a class='link' href='/admin/conversations?key={kf}'>← Volver al panel de conversaciones</a></div>"
    )
    extra = (
        "<style>"
        ".state{border-radius:14px;padding:14px 16px;margin-bottom:18px;font-size:14px}"
        ".state.on{background:#dcfce7;color:#166534}"
        ".state.off{background:#fef3c7;color:#92400e}"
        ".flash{background:#e0f2fe;color:#075985;border-radius:12px;padding:10px 14px;margin-bottom:14px;font-size:13px}"
        ".promoform{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}"
        ".promoform label{display:flex;flex-direction:column;gap:5px;font-size:12px;color:#6b6b74;font-weight:600}"
        ".promoform input{border:1px solid #d7d7de;border-radius:9px;padding:9px 11px;font-size:14px;min-width:230px}"
        ".promoform .apply{border:0;background:#ef2c8f;color:#fff;border-radius:9px;padding:11px 18px;"
        "font-size:14px;font-weight:700;cursor:pointer}"
        "button.del{border:1px solid #e11d48;color:#e11d48;background:#fff;border-radius:999px;"
        "padding:6px 14px;font-size:12px;font-weight:600;cursor:pointer}"
        "button.del:hover{background:#e11d48;color:#fff}"
        "</style>"
    )
    return _admin_page("Promociones — Tropical Glitz", body + extra)


@router.get("/admin/promos", response_class=HTMLResponse)
async def admin_promos(
    key: str = "", msg: str = "", session: AsyncSession = Depends(get_session)
) -> HTMLResponse:
    if not _settings.admin_token:
        return HTMLResponse(_admin_locked("Falta configurar ADMIN_TOKEN en Render."), status_code=503)
    if key != _settings.admin_token:
        return HTMLResponse(
            _admin_locked("Token inválido. Abre esta página con ?key=TU_TOKEN al final de la URL."),
            status_code=401,
        )
    from app.services import promos

    rows = await promos.list_promos(session)
    return HTMLResponse(_promos_page(key, rows, msg))


@router.post("/admin/promos/add")
async def admin_promos_add(request: Request, session: AsyncSession = Depends(get_session)):
    form = await _form_data(request)
    key = str(form.get("key") or "")
    if not _settings.admin_token or key != _settings.admin_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    from app.services import promos

    code = str(form.get("code") or "").strip()[:40]
    description = str(form.get("description") or "").strip()[:200]
    if code:
        await promos.add_promo(session, code=code, description=description)
        note = f"Código {code} activado."
    else:
        note = "Escribe un código."
    from urllib.parse import quote

    return RedirectResponse(
        f"/admin/promos?key={quote(key)}&msg={quote(note)}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/admin/promos/delete")
async def admin_promos_delete(request: Request, session: AsyncSession = Depends(get_session)):
    form = await _form_data(request)
    key = str(form.get("key") or "")
    if not _settings.admin_token or key != _settings.admin_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    from app.services import promos

    try:
        promo_id = int(str(form.get("promo_id") or "0"))
    except ValueError:
        promo_id = 0
    if promo_id:
        await promos.remove_promo(session, promo_id=promo_id)
    from urllib.parse import quote

    return RedirectResponse(
        f"/admin/promos?key={quote(key)}&msg={quote('Código desactivado. El asistente vuelve a responder que no hay promoción.')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


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
