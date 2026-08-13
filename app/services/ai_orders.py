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
        "created_at": order.get("created_at"),
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
            VALUES (:order_id, :order_name, :session_id, :created_at, :total,
                    :ai_revenue, :currency, :financial_status, :cancelled, CAST(:items AS jsonb), now())
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
        {**row, "items": json.dumps(row.get("items") or [], ensure_ascii=False)},
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
