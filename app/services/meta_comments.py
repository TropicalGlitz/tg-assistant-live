"""Comentarios de Instagram y Facebook: ingesta por webhook, clasificación y borrador.

A diferencia de YouTube aquí NO sondeamos: Meta empuja el evento en el momento.
El resto del flujo es idéntico — se clasifica, solo las preguntas reciben
borrador, y nada se publica sin que un humano lo apruebe en el panel.

Reutiliza a propósito `classify` y `draft_reply` de yt_comments: es el mismo
criterio y el mismo tono para las tres fuentes.
"""
from __future__ import annotations

import datetime as _dt
import logging
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import meta
from app.services.yt_comments import ANSWERABLE, classify, draft_reply

_log = logging.getLogger("meta_comments")

_DDL = """
CREATE TABLE IF NOT EXISTS meta_comments (
    comment_id   TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    page_id      TEXT,
    ig_id        TEXT,
    post_id      TEXT,
    permalink    TEXT,
    author       TEXT,
    body         TEXT NOT NULL DEFAULT '',
    published_at TIMESTAMPTZ,
    kind         TEXT NOT NULL DEFAULT 'pending',
    draft        TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    reply_text   TEXT,
    reply_id     TEXT,
    handled_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS meta_comments_status_idx
    ON meta_comments (status, published_at DESC);
"""

_ensured = False


async def _ensure(session: AsyncSession) -> None:
    global _ensured
    if _ensured:
        return
    for stmt in filter(None, (s.strip() for s in _DDL.split(";"))):
        await session.execute(text(stmt))
    await session.commit()
    _ensured = True


def _as_dt(value: Any) -> _dt.datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):  # FB manda epoch en algunos campos
        return _dt.datetime.fromtimestamp(float(value), _dt.timezone.utc)
    txt = str(value).strip()
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(txt)
    except ValueError:
        try:
            parsed = _dt.datetime.strptime(txt, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


def _parse(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Aplana el webhook a una lista de comentarios nuevos.

    Instagram manda field='comments'; Facebook manda field='feed' con
    item='comment'. Se ignora todo lo demás (likes, reacciones, ediciones).
    """
    out: list[dict[str, Any]] = []
    for entry in payload.get("entry") or []:
        entry_id = str(entry.get("id") or "")
        for ch in entry.get("changes") or []:
            field = ch.get("field")
            v = ch.get("value") or {}

            if field == "comments":  # Instagram
                frm = v.get("from") or {}
                out.append({
                    "comment_id": str(v.get("id") or ""),
                    "source": "ig",
                    "page_id": "",
                    "ig_id": entry_id,
                    "post_id": str((v.get("media") or {}).get("id") or ""),
                    "permalink": "",
                    "author": frm.get("username") or "",
                    "author_id": str(frm.get("id") or ""),
                    "body": v.get("text") or "",
                    "published_at": _as_dt(v.get("timestamp")),
                })

            elif field == "feed" and v.get("item") == "comment":  # Facebook
                if v.get("verb") not in (None, "add"):
                    continue  # ediciones y borrados no nos interesan
                frm = v.get("from") or {}
                out.append({
                    "comment_id": str(v.get("comment_id") or ""),
                    "source": "fb",
                    "page_id": entry_id,
                    "ig_id": "",
                    "post_id": str(v.get("post_id") or ""),
                    "permalink": v.get("permalink_url") or "",
                    "author": frm.get("name") or "",
                    "author_id": str(frm.get("id") or ""),
                    "body": v.get("message") or "",
                    "published_at": _as_dt(v.get("created_time")),
                })
    return [c for c in out if c["comment_id"]]


async def ingest(session: AsyncSession, payload: dict[str, Any]) -> int:
    """Guarda los comentarios del webhook y redacta los que valen la pena."""
    await _ensure(session)
    items = _parse(payload)
    if not items:
        return 0

    # Nuestros propios comentarios (respuestas del negocio) no entran a la bandeja.
    conn = await meta.connection(session)
    ours = set()
    for p in (conn or {}).get("pages") or []:
        ours.add(p.get("page_id") or "")
        ours.add(p.get("ig_id") or "")
    items = [c for c in items if c.get("author_id") not in ours]
    if not items:
        return 0

    saved = 0
    for c in items:
        # El webhook a veces llega sin el texto; lo pedimos a la Graph API.
        if not c["body"]:
            try:
                full = await meta.fetch_comment(
                    session, c["comment_id"],
                    page_id=c["page_id"], ig_id=c["ig_id"],
                )
                c["body"] = full.get("text") or full.get("message") or ""
                if not c["author"]:
                    frm = full.get("from") or {}
                    c["author"] = full.get("username") or frm.get("name") or ""
                if not c["published_at"]:
                    c["published_at"] = _as_dt(
                        full.get("timestamp") or full.get("created_time")
                    )
            except Exception:  # noqa: BLE001
                _log.warning("No se pudo completar el comentario %s", c["comment_id"])

        kind = await classify(c["body"])
        status = "pending" if kind in ANSWERABLE else "archived"
        row = {k: v for k, v in c.items() if k != "author_id"}
        res = await session.execute(
            text(
                "INSERT INTO meta_comments (comment_id, source, page_id, ig_id, post_id,"
                " permalink, author, body, published_at, kind, draft, status) "
                "VALUES (:comment_id, :source, :page_id, :ig_id, :post_id, :permalink,"
                " :author, :body, :published_at, :kind, '', :status) "
                "ON CONFLICT (comment_id) DO NOTHING"
            ),
            {**row, "kind": kind, "status": status},
        )
        await session.commit()
        if res.rowcount:
            saved += 1

    await draft_pending(session)
    return saved


async def draft_pending(session: AsyncSession, limit: int = 8) -> int:
    """Redacta los borradores que faltan. Commit por borrador: nada se pierde."""
    rows = (await session.execute(
        text(
            "SELECT comment_id, body FROM meta_comments "
            "WHERE status = 'pending' AND coalesce(draft, '') = '' "
            "ORDER BY published_at DESC NULLS LAST LIMIT :lim"
        ),
        {"lim": limit},
    )).all()
    await session.commit()   # no dejar la transacción abierta durante el modelo
    done = 0
    for cid, body in rows:
        try:
            draft = await draft_reply(session, body)
        except Exception:  # noqa: BLE001
            _log.exception("Falló el borrador de %s", cid)
            continue
        await session.execute(
            text("UPDATE meta_comments SET draft = :d WHERE comment_id = :c"),
            {"d": draft, "c": cid},
        )
        await session.commit()
        done += 1
    return done


async def mark_replied(session: AsyncSession, cid: str, body: str, reply_id: str) -> None:
    await session.execute(
        text(
            "UPDATE meta_comments SET status='replied', reply_text=:b, reply_id=:r,"
            " handled_at=now() WHERE comment_id=:c"
        ),
        {"b": body, "r": reply_id, "c": cid},
    )
    await session.commit()


async def set_status(session: AsyncSession, cid: str, status: str) -> None:
    await session.execute(
        text("UPDATE meta_comments SET status=:s, handled_at=now() WHERE comment_id=:c"),
        {"s": status, "c": cid},
    )
    await session.commit()


async def regenerate(session: AsyncSession, cid: str) -> str:
    row = (await session.execute(
        text("SELECT body FROM meta_comments WHERE comment_id = :c"), {"c": cid}
    )).first()
    await session.commit()
    if not row:
        return ""
    draft = await draft_reply(session, row[0])
    await session.execute(
        text("UPDATE meta_comments SET draft = :d WHERE comment_id = :c"),
        {"d": draft, "c": cid},
    )
    await session.commit()
    return draft


async def routing(session: AsyncSession, cid: str) -> dict[str, str]:
    """De qué página/cuenta es el comentario, para elegir el token correcto."""
    row = (await session.execute(
        text("SELECT source, page_id, ig_id FROM meta_comments WHERE comment_id=:c"),
        {"c": cid},
    )).mappings().first()
    await session.commit()
    return dict(row) if row else {}


async def counters(session: AsyncSession) -> dict[str, int]:
    await _ensure(session)
    rows = (await session.execute(
        text("SELECT status, count(*) FROM meta_comments GROUP BY status")
    )).all()
    await session.commit()
    return {r[0]: int(r[1]) for r in rows}


async def listing(session: AsyncSession, status: str = "pending", limit: int = 60) -> list[dict]:
    await _ensure(session)
    sql = (
        "SELECT comment_id, source, page_id, ig_id, post_id, permalink, author, body,"
        " published_at, kind, draft, status, reply_text, handled_at FROM meta_comments "
    )
    params: dict[str, Any] = {"lim": limit}
    if status and status != "all":
        sql += "WHERE status = :st "
        params["st"] = status
    sql += "ORDER BY published_at DESC NULLS LAST LIMIT :lim"
    rows = (await session.execute(text(sql), params)).mappings().all()
    await session.commit()
    return [dict(r) for r in rows]
