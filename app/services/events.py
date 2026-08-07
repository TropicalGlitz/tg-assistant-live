"""Eventos del widget para el panel de control (embudo y atribución de ventas).

Guardamos eventos ligeros del chat — sobre todo `add_to_cart` cuando el cliente
agrega al carrito un producto DESDE la conversación. Junto con la etiqueta que el
widget pone en el carrito (`_tg_ai_session`), esto permite atribuir ventas al AI.

Escribir un evento NUNCA debe romper el chat: si algo falla, se ignora.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("events")

# Tipos de evento aceptados por /event (lista blanca).
ALLOWED_TYPES = {"add_to_cart", "view_cart", "open_contact", "card_shown"}

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS chat_events (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT,
    type TEXT NOT NULL,
    title TEXT,
    variant_id TEXT,
    price NUMERIC,
    meta JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_events_created_idx ON chat_events (created_at DESC);
CREATE INDEX IF NOT EXISTS chat_events_session_idx ON chat_events (session_id, created_at);
CREATE INDEX IF NOT EXISTS chat_events_type_idx ON chat_events (type, created_at);
"""

_ensured = False


async def _ensure(session: AsyncSession) -> None:
    global _ensured
    if _ensured:
        return
    try:
        for stmt in [s.strip() for s in CREATE_SQL.split(";") if s.strip()]:
            await session.execute(text(stmt))
        await session.commit()
        _ensured = True
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo asegurar la tabla chat_events")


async def log_event(
    session: AsyncSession,
    *,
    session_id: str | None,
    type: str,
    title: str | None = None,
    variant_id: str | None = None,
    price: float | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    await _ensure(session)
    try:
        await session.execute(
            text(
                """
                INSERT INTO chat_events (session_id, type, title, variant_id, price, meta)
                VALUES (:sid, :type, :title, :variant_id, :price, CAST(:meta AS jsonb))
                """
            ),
            {
                "sid": (session_id or None),
                "type": type,
                "title": title,
                "variant_id": (str(variant_id) if variant_id is not None else None),
                "price": price,
                "meta": json.dumps(meta or {}, ensure_ascii=False),
            },
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo registrar el evento (se ignora)")


async def sessions_with_add_to_cart(session: AsyncSession) -> set[str]:
    """Conjunto de session_id que agregaron al menos un producto al carrito desde el chat."""
    await _ensure(session)
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT session_id FROM chat_events
                    WHERE type = 'add_to_cart' AND session_id IS NOT NULL
                    """
                )
            )
        ).all()
        return {r[0] for r in rows if r[0]}
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo leer sesiones con add_to_cart")
        return set()


async def add_to_cart_items(session: AsyncSession, limit: int = 500) -> list[dict[str, Any]]:
    """Últimos eventos de add_to_cart (para la sección de embudo/detalle)."""
    await _ensure(session)
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT session_id, title, variant_id, price, created_at
                    FROM chat_events
                    WHERE type = 'add_to_cart'
                    ORDER BY created_at DESC
                    LIMIT :limit
                    """
                ),
                {"limit": limit},
            )
        ).mappings().all()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo leer add_to_cart_items")
        return []


async def stats(session: AsyncSession) -> dict[str, int]:
    """Conteos rápidos de eventos para los KPIs."""
    await _ensure(session)
    try:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        count(*) FILTER (WHERE type = 'add_to_cart') AS add_to_cart,
                        count(DISTINCT session_id) FILTER (WHERE type = 'add_to_cart') AS atc_sessions
                    FROM chat_events
                    """
                )
            )
        ).mappings().first()
        return {k: int(v or 0) for k, v in dict(row).items()} if row else {}
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo calcular stats de eventos")
        return {}
