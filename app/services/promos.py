"""Códigos de promoción controlados desde el panel de control.

Regla de negocio (pedida por el dueño): el asistente NUNCA inventa ni asume
descuentos. Por defecto responde que no hay código de promoción vigente. Solo
cuando el dueño agrega un código aquí (desde /admin/promos) el AI lo menciona.

Se reutiliza la tabla `promotions` que ya existe (migración 003). Este módulo
solo maneja los campos que el dueño pidió: CÓDIGO + DESCRIPCIÓN. La promo vive
hasta que él la borra del panel; no hay fechas que mantener.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("promos")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS promotions (
    id           BIGSERIAL PRIMARY KEY,
    code         TEXT,
    title        TEXT NOT NULL,
    description  TEXT,
    active       BOOLEAN NOT NULL DEFAULT true,
    starts_at    TIMESTAMPTZ,
    ends_at      TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_promotions_window ON promotions (active, starts_at, ends_at);
"""

_ensured = False


async def ensure_table(session: AsyncSession) -> None:
    """Crea la tabla si falta. Idempotente y barato (solo la 1ª vez por proceso)."""
    global _ensured
    if _ensured:
        return
    try:
        for stmt in [s.strip() for s in CREATE_SQL.split(";") if s.strip()]:
            await session.execute(text(stmt))
        await session.commit()
        _ensured = True
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo asegurar la tabla promotions")


async def list_promos(session: AsyncSession) -> list[dict[str, Any]]:
    """Todas las promos guardadas (para el panel), más nuevas primero."""
    await ensure_table(session)
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, code, title, description, active, created_at
                    FROM promotions
                    ORDER BY created_at DESC
                    LIMIT 50;
                    """
                )
            )
        ).mappings().all()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        _log.exception("No se pudieron leer las promociones")
        return []


async def active_codes(session: AsyncSession) -> list[dict[str, str]]:
    """Códigos vigentes para el chat: activos, con código, y dentro de fechas si
    las tuvieran (las promos creadas desde el panel no usan fechas).

    Nunca lanza: si la tabla no existe o la BD falla, el AI simplemente responde
    que no hay promoción — que es la respuesta segura."""
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT code, title, description FROM promotions
                    WHERE active = true
                      AND code IS NOT NULL AND btrim(code) <> ''
                      AND (starts_at IS NULL OR starts_at <= now())
                      AND (ends_at   IS NULL OR ends_at   >= now())
                    ORDER BY created_at DESC
                    LIMIT 3;
                    """
                )
            )
        ).mappings().all()
        return [
            {
                "code": (r["code"] or "").strip(),
                "description": (r["description"] or r["title"] or "").strip(),
            }
            for r in rows
        ]
    except Exception:  # noqa: BLE001
        _log.warning("No se pudieron leer las promos activas; se asume ninguna")
        return []


async def add_promo(session: AsyncSession, *, code: str, description: str) -> None:
    await ensure_table(session)
    code = (code or "").strip()
    description = (description or "").strip()
    if not code:
        return
    await session.execute(
        text(
            """
            INSERT INTO promotions (code, title, description, active, starts_at, ends_at)
            VALUES (:c, :d, :d, true, NULL, NULL);
            """
        ),
        {"c": code, "d": description or code},
    )
    await session.commit()


async def set_active(session: AsyncSession, *, promo_id: int, active: bool) -> None:
    await ensure_table(session)
    await session.execute(
        text("UPDATE promotions SET active = :a WHERE id = :i;"),
        {"a": active, "i": promo_id},
    )
    await session.commit()


async def remove_promo(session: AsyncSession, *, promo_id: int) -> None:
    """Baja lógica: se desactiva y se le quita el código para que jamás vuelva a
    salir en el chat, pero queda el registro histórico en la tabla."""
    await ensure_table(session)
    await session.execute(
        text("UPDATE promotions SET active = false, code = NULL WHERE id = :i;"),
        {"i": promo_id},
    )
    await session.commit()


def context_block(promos: list[dict[str, str]]) -> str:
    """Línea que se inyecta en el CONTEXT del modelo. Siempre presente: o dice
    que NO hay promoción, o lista la(s) vigente(s). Así el AI nunca tiene que
    adivinar."""
    if not promos:
        # OJO: "no hay promo" se refiere a las CAMPAÑAS temporales del panel. El
        # código WELCOME (10% de bienvenida, un uso por cliente) es permanente y
        # vive en el system prompt, no aquí — por eso se nombra explícitamente:
        # sin esta aclaración el modelo leía "no hay ningún código" y lo negaba.
        return (
            '{"type":"promotions","active":false,'
            '"instruction":"There is NO limited-time sale or campaign code running right now. '
            'This does NOT include the standing WELCOME code (10% off, one use per customer, '
            'first order) described in your instructions, which is always available and which you '
            'SHOULD give when the customer asks about a discount or says they subscribed and '
            'never got their code. Beyond WELCOME, NEVER invent, guess or imply a code, '
            'percentage or sale."}'
        )
    items = "; ".join(
        f"{p['code']}" + (f" ({p['description']})" if p.get("description") else "")
        for p in promos
    )
    return (
        '{"type":"promotions","active":true,"codes":"'
        + items.replace('"', "'")
        + '","instruction":"This promo code IS currently active. Give the customer the exact code '
        'as written when they ask about discounts, and also mention it briefly when you recommend '
        'a product. Do not invent any other code, percentage or condition beyond what is written here. '
        'The standing WELCOME code (10% off, first order only) still exists as well; codes '
        'generally cannot be stacked with each other."}'
    )
