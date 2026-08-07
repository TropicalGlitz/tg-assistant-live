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
    session: AsyncSession,
    limit: int = 500,
    mode: str | None = None,
    *,
    since: Any = None,
    until: Any = None,
) -> list[dict[str, Any]]:
    await _ensure(session)
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if mode in ("handoff", "catalog", "general"):
        clauses.append("mode = :mode")
        params["mode"] = mode
    if since is not None and until is not None:
        clauses.append("created_at >= :since AND created_at < :until")
        params["since"] = since
        params["until"] = until
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
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


async def daily_series(session: AsyncSession, since: Any, until: Any) -> list[dict[str, Any]]:
    """Serie diaria de conversaciones (total y derivadas a humano) para los gráficos."""
    await _ensure(session)
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT date_trunc('day', created_at)::date AS day,
                           count(*) AS total,
                           count(*) FILTER (WHERE handoff) AS handoff
                    FROM chat_logs
                    WHERE created_at >= :since AND created_at < :until
                    GROUP BY 1
                    ORDER BY 1
                    """
                ),
                {"since": since, "until": until},
            )
        ).mappings().all()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo calcular la serie diaria")
        return []


async def recent_history(
    session: AsyncSession, session_id: str | None, limit: int = 6
) -> list[dict[str, str]]:
    """Últimos intercambios de esta sesión como mensajes para Claude (memoria).

    Devuelve pares user/assistant en orden cronológico para que el asistente
    continúe la conversación en lugar de tratar cada mensaje por separado.
    """
    if not session_id:
        return []
    await _ensure(session)
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT message, answer FROM chat_logs
                    WHERE session_id = :sid
                    ORDER BY created_at DESC
                    LIMIT :n
                    """
                ),
                {"sid": session_id, "n": limit},
            )
        ).mappings().all()
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo leer el historial de la sesión")
        return []
    msgs: list[dict[str, str]] = []
    for r in reversed(rows):  # cronológico
        if r.get("message"):
            msgs.append({"role": "user", "content": r["message"]})
        if r.get("answer"):
            msgs.append({"role": "assistant", "content": r["answer"]})
    return msgs


async def session_transcript(
    session: AsyncSession, session_id: str | None, limit: int = 200
) -> list[dict[str, str]]:
    """Toda la conversación de esta sesión como pares user/assistant en orden
    cronológico, para adjuntarla al email de contacto que recibe soporte."""
    if not session_id:
        return []
    await _ensure(session)
    try:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT message, answer FROM chat_logs
                    WHERE session_id = :sid
                    ORDER BY created_at ASC
                    LIMIT :n
                    """
                ),
                {"sid": session_id, "n": limit},
            )
        ).mappings().all()
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo leer la transcripción de la sesión")
        return []
    msgs: list[dict[str, str]] = []
    for r in rows:
        if r.get("message"):
            msgs.append({"role": "user", "content": r["message"]})
        if r.get("answer"):
            msgs.append({"role": "assistant", "content": r["answer"]})
    return msgs


async def stats(
    session: AsyncSession, *, since: Any = None, until: Any = None
) -> dict[str, int]:
    await _ensure(session)
    where = ""
    params: dict[str, Any] = {}
    if since is not None and until is not None:
        where = "WHERE created_at >= :since AND created_at < :until"
        params = {"since": since, "until": until}
    row = (
        await session.execute(
            text(
                f"""
                SELECT
                    count(*) AS total,
                    count(DISTINCT session_id) AS sessions,
                    count(DISTINCT session_id) FILTER (WHERE handoff) AS handoff_sessions,
                    count(*) FILTER (WHERE mode = 'general') AS general,
                    count(*) FILTER (WHERE mode = 'handoff') AS handoff,
                    count(*) FILTER (WHERE mode = 'catalog') AS catalog
                FROM chat_logs
                {where}
                """
            ),
            params,
        )
    ).mappings().first()
    return {k: int(v or 0) for k, v in dict(row).items()} if row else {}
