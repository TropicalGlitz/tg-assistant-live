"""Registro de conversaciones del asistente para supervisión.

Cada intercambio (pregunta del cliente + respuesta del AI) se guarda en la tabla
`conversations`. Sirve para leer qué está respondiendo el asistente, detectar
dónde conviene mejorar, y alimentar la base de conocimiento con mejores respuestas.

La tabla se crea con sql/conversations.sql (o el CREATE de abajo). Escribir un log
NUNCA debe romper el chat: si algo falla, se ignora silenciosamente.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_log = logging.getLogger("conversations")

# Nombre propio para evitar chocar con una tabla `conversations` preexistente
# (de las migraciones iniciales, con otro esquema). Este es el log del asistente.
CREATE_SQL = """
CREATE TABLE IF NOT EXISTS chat_logs (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT,
    message TEXT NOT NULL,
    answer TEXT NOT NULL,
    mode TEXT,
    handoff BOOLEAN DEFAULT false,
    sources JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS chat_logs_created_idx ON chat_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS chat_logs_session_idx ON chat_logs (session_id, created_at);
"""


_ensured = False


async def _ensure(session: AsyncSession) -> None:
    """Crea la tabla/índices si no existen (idempotente, una vez por proceso)."""
    global _ensured
    if _ensured:
        return
    try:
        for stmt in [s.strip() for s in CREATE_SQL.split(";") if s.strip()]:
            await session.execute(text(stmt))
        await session.commit()
        _ensured = True
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo asegurar la tabla conversations")


def classify(handoff: bool, sources: list[Any] | None) -> str:
    """Etiqueta legible del tipo de respuesta, para supervisar de un vistazo."""
    if handoff:
        return "handoff"
    if sources:
        return "catalog"  # respondió con catálogo/FAQ (grounded)
    return "general"  # respondió con conocimiento general de Claude


async def log_exchange(
    session: AsyncSession,
    *,
    session_id: str | None,
    message: str,
    answer: str,
    handoff: bool,
    sources: list[Any] | None,
) -> None:
    await _ensure(session)
    try:
        await session.execute(
            text(
                """
                INSERT INTO chat_logs (session_id, message, answer, mode, handoff, sources)
                VALUES (:sid, :msg, :ans, :mode, :handoff, CAST(:sources AS jsonb))
                """
            ),
            {
                "sid": (session_id or None),
                "msg": message,
                "ans": answer,
                "mode": classify(handoff, sources),
                "handoff": bool(handoff),
                "sources": json.dumps(sources or [], ensure_ascii=False),
            },
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo registrar la conversación (se ignora)")


async def fetch_recent(
    session: AsyncSession, limit: int = 500, mode: str | None = None
) -> list[dict[str, Any]]:
    await _ensure(session)
    where = ""
    params: dict[str, Any] = {"limit": limit}
    if mode in ("handoff", "catalog", "general"):
        where = "WHERE mode = :mode"
        params["mode"] = mode
    rows = (
        await session.execute(
            text(
                f"""
                SELECT id, session_id, message, answer, mode, handoff, sources, created_at
                FROM chat_logs
                {where}
                ORDER BY created_at DESC
                LIMIT :limit
                """
            ),
            params,
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def stats(session: AsyncSession) -> dict[str, int]:
    await _ensure(session)
    row = (
        await session.execute(
            text(
                """
                SELECT
                    count(*) AS total,
                    count(*) FILTER (WHERE created_at > now() - interval '24 hours') AS last24h,
                    count(*) FILTER (WHERE mode = 'general') AS general,
                    count(*) FILTER (WHERE mode = 'handoff') AS handoff,
                    count(*) FILTER (WHERE mode = 'catalog') AS catalog
                FROM chat_logs
                """
            )
        )
    ).mappings().first()
    return {k: int(v or 0) for k, v in dict(row).items()} if row else {}
