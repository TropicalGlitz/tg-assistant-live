"""Órdenes atribuidas al AI, capturadas por webhook en el momento de la compra.

POR QUÉ EXISTE (hallazgo de la auditoría): el panel calculaba las ventas del AI
escaneando el historial de órdenes con la Admin API, con un tope de 4 páginas ×
250 = 1000 órdenes. Con el volumen real de la tienda (~78 órdenes/día) ese tope
solo alcanza ~13 días, así que la vista de 30 días (la de por defecto) veía menos
de la mitad del período y la de 90 días apenas ~14%. Resultado: las ventas del AI
salían SUBESTIMADAS.

Con el webhook `orders/create` guardamos la orden en el momento en que ocurre —
sin paginación, sin tope y sin depender de la API en cada carga del panel. El
escaneo por API se conserva solo como respaldo para el historial anterior.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("ai_orders")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS ai_orders (
    order_id      TEXT PRIMARY KEY,
    order_name    TEXT,
    session_id    TEXT,
    created_at    TIMESTAMPTZ NOT NULL,
    total         NUMERIC NOT NULL DEFAULT 0,
    ai_revenue    NUMERIC NOT NULL DEFAULT 0,
    currency      TEXT,
    financial_status TEXT,
    cancelled     BOOLEAN NOT NULL DEFAULT false,
    items         JSONB,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ai_orders_created_idx ON ai_orders (created_at DESC);
CREATE INDEX IF NOT EXISTS ai_orders_session_idx ON ai_orders (session_id);
"""

_ensured = False


async def ensure_table(session: AsyncSession) -> None:
    global _ensured
    if _ensured:
        return
    try:
        for stmt in [s.strip() for s in CREATE_SQL.split(";") if s.strip()]:
            await session.execute(text(stmt))
        await session.commit()
        _ensured = True
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo asegurar la tabla ai_orders")


def _as_datetime(value: Any) -> _dt.datetime:
    """Shopify manda `created_at` como texto ISO ("2026-08-12T08:48:52-04:00").

    asyncpg NO acepta texto para una columna TIMESTAMPTZ: exige un datetime. Sin
    esta conversión el INSERT reventaba con DataError y la orden se perdía — que
    es exactamente lo que estaba pasando (el webhook se tragaba el error y el
    backfill lo contaba como "no guardada").
    """
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    txt = str(value or "").strip()
    if txt:
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        try:
            parsed = _dt.datetime.fromisoformat(txt)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)
        except ValueError:
            _log.warning("Fecha de orden ilegible (%r); se usa la hora actual", value)
    return _dt.datetime.now(_dt.timezone.utc)


def _note_attrs(order: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for na in order.get("note_attributes") or []:
        name = na.get("name")
        if name:
            out[str(name)] = str(na.get("value") or "")
    return out


def parse_order(order: dict[str, Any]) -> dict[str, Any] | None:
    """Convierte el payload de Shopify en una fila de ai_orders.

    Devuelve None si la orden NO viene del chat (sin las marcas `_tg_ai*`), que
    es el caso de la gran mayoría de las órdenes de la tienda.
    """
    na = _note_attrs(order)
    sid = na.get("_tg_ai_session") or ""
    if not sid and na.get("_tg_ai") != "1":
        return None

    ai_variants = {v for v in (na.get("_tg_ai_variants") or "").split(",") if v}
    total = float(order.get("current_total_price") or order.get("total_price") or 0)
    direct = 0.0
    items: list[dict[str, Any]] = []
    for li in order.get("line_items") or []:
        vid = str(li.get("variant_id"))
        line_total = float(li.get("price") or 0) * int(li.get("quantity") or 0)
        # Si sabemos qué variantes agregó desde el chat, solo esas cuentan como
        # venta del AI; si no lo sabemos, se atribuye la orden completa.
        is_ai = (vid in ai_variants) if ai_variants else True
        if is_ai:
            direct += line_total
        items.append(
            {"title": li.get("title"), "qty": li.get("quantity"), "variant_id": vid, "ai": is_ai}
        )
    return {
        "order_id": str(order.get("id")),
        "order_name": order.get("name"),
        "session_id": sid or None,
        "created_at": _as_datetime(order.get("created_at")),
        "total": round(total, 2),
        "ai_revenue": round(direct, 2),
        "currency": order.get("currency") or "USD",
        "financial_status": order.get("financial_status"),
        "cancelled": bool(order.get("cancelled_at")),
        "items": items,
    }


async def upsert(session: AsyncSession, row: dict[str, Any]) -> None:
    """Guarda/actualiza la orden. Idempotente por order_id: Shopify puede
    reenviar el mismo webhook y `orders/updated` trae cambios (pago, cancelación)."""
    await ensure_table(session)
    await session.execute(
        text(
            """
            INSERT INTO ai_orders (order_id, order_name, session_id, created_at, total,
                                   ai_revenue, currency, financial_status, cancelled, items, updated_at)
            VALUES (:order_id, :order_name, :session_id, :created_at, CAST(:total AS NUMERIC),
                    CAST(:ai_revenue AS NUMERIC), :currency, :financial_status, :cancelled,
                    CAST(:items AS jsonb), now())
            ON CONFLICT (order_id) DO UPDATE SET
                order_name = EXCLUDED.order_name,
                session_id = COALESCE(EXCLUDED.session_id, ai_orders.session_id),
                total = EXCLUDED.total,
                ai_revenue = EXCLUDED.ai_revenue,
                financial_status = EXCLUDED.financial_status,
                cancelled = EXCLUDED.cancelled,
                items = EXCLUDED.items,
                updated_at = now();
            """
        ),
        {
            **row,
            "total": str(row.get("total") or 0),
            "ai_revenue": str(row.get("ai_revenue") or 0),
            "items": json.dumps(row.get("items") or [], ensure_ascii=False),
        },
    )
    await session.commit()


async def fetch_range(
    session: AsyncSession, since: Any, until: Any
) -> list[dict[str, Any]]:
    """Órdenes del AI en un rango. Sin tope de paginación: es nuestra tabla."""
    await ensure_table(session)
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT order_id, order_name, session_id, created_at, total, ai_revenue,
                           currency, financial_status, cancelled, items
                    FROM ai_orders
                    WHERE created_at >= :since AND created_at < :until
                    ORDER BY created_at DESC
                    """
                ),
                {"since": since, "until": until},
            )
        ).mappings().all()
    except Exception:  # noqa: BLE001
        _log.exception("No se pudieron leer las órdenes del AI")
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        out.append(
            {
                "order": d.get("order_name"),
                "session_id": d.get("session_id"),
                "created_at": str(d.get("created_at") or "")[:10],
                "total": float(d.get("total") or 0),
                "ai_revenue": float(d.get("ai_revenue") or 0),
                "currency": d.get("currency") or "USD",
                "financial_status": d.get("financial_status"),
                "cancelled": bool(d.get("cancelled")),
                "items": d.get("items") or [],
            }
        )
    return out


async def count_all(session: AsyncSession) -> int:
    """Cuántas órdenes del AI tenemos capturadas (para saber si el webhook ya
    está alimentando la tabla o todavía dependemos del escaneo por API)."""
    await ensure_table(session)
    try:
        row = (await session.execute(text("SELECT count(*) FROM ai_orders"))).first()
        return int(row[0]) if row else 0
    except Exception:  # noqa: BLE001
        return 0


async def backfill(
    session: AsyncSession, *, days: int = 120, max_pages: int = 40
) -> dict[str, Any]:
    """Rellena `ai_orders` con el historial que Shopify todavía nos deja leer.

    El webhook cubre de aquí en adelante; esto recupera lo de antes. Se recorre
    página por página (250 órdenes cada una) y se guardan SOLO las del chat, sin
    acumular nada en memoria: la instancia tiene 512MB y el historial es largo.
    """
    import datetime as _dt

    import httpx

    from app.core.config import get_settings
    from app.services.orders import _next_link  # noqa: PLC2701

    settings = get_settings()
    await ensure_table(session)
    api = (
        f"https://{settings.shopify_shop_domain}"
        f"/admin/api/{settings.shopify_api_version}"
    )
    headers = {"X-Shopify-Access-Token": settings.shopify_admin_token}
    since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
    params = {
        "status": "any",
        "limit": "250",
        "created_at_min": since.isoformat(),
        "fields": (
            "id,name,created_at,total_price,current_total_price,financial_status,"
            "cancelled_at,line_items,note_attributes,currency"
        ),
    }
    scanned = 0
    stored = 0
    pages = 0
    truncated = False
    # Diagnóstico: si `stored` sale 0 hay que poder distinguir "nadie compró
    # todavía desde el chat" de "la etiqueta se pierde antes de llegar a la
    # orden". Para eso contamos cuántas órdenes traen CUALQUIER note_attribute,
    # cuántas traen las del sistema REP anterior y cuántas las nuestras, además
    # del rango de fechas que realmente alcanzamos a mirar.
    con_atributos = 0
    con_rep = 0
    con_tg = 0
    primera = ""
    ultima = ""
    url: str | None = f"{api}/orders.json"
    async with httpx.AsyncClient(headers=headers, timeout=60) as client:
        while url and pages < max_pages:
            r = await client.get(url, params=params if pages == 0 else None)
            r.raise_for_status()
            batch = r.json().get("orders", [])
            scanned += len(batch)
            for o in batch:
                creado = str(o.get("created_at") or "")[:10]
                if creado:
                    if not ultima or creado > ultima:
                        ultima = creado
                    if not primera or creado < primera:
                        primera = creado
                na = _note_attrs(o)
                if na:
                    con_atributos += 1
                    if any(k.startswith("rep_") for k in na):
                        con_rep += 1
                    if any(k.startswith("_tg_ai") for k in na):
                        con_tg += 1
                row = parse_order(o)
                if not row:
                    continue
                try:
                    await upsert(session, row)
                    stored += 1
                except Exception:  # noqa: BLE001
                    _log.exception("No se pudo guardar la orden %s en el backfill", row.get("order_name"))
            pages += 1
            url = _next_link(r.headers.get("link", ""))
        if url:
            truncated = True
    total = await count_all(session)
    _log.info("Backfill de órdenes del AI: %s escaneadas, %s guardadas", scanned, stored)
    return {
        "scanned": scanned,
        "stored": stored,
        "pages": pages,
        "truncated": truncated,
        "total_en_tabla": total,
        "dias": days,
        "diagnostico": {
            "ordenes_con_algun_atributo": con_atributos,
            "con_etiqueta_rep_vieja": con_rep,
            "con_etiqueta_tg_ai": con_tg,
            "desde": primera,
            "hasta": ultima,
        },
    }
