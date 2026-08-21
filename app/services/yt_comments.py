"""Bandeja de comentarios de YouTube: sondeo, clasificación y borrador.

Flujo: cada pocos minutos traemos los comentarios nuevos del canal, descartamos
el ruido (elogios, emojis, spam) con una clasificación barata, y solo para las
preguntas de verdad generamos un borrador con el MISMO motor RAG del chat.
Nada se publica solo: el borrador queda pendiente hasta que un humano lo aprueba
desde /admin/youtube.

La respuesta de YouTube es pública y permanente, así que el borrador es más
corto y más prudente que una respuesta de chat: sin enlaces de producto, sin
promesas de precio o stock, y derivando a soporte cuando el tema es de una
orden concreta.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import logging
import re
from typing import Any

from anthropic import AsyncAnthropic
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.services import rag, youtube

_log = logging.getLogger("yt_comments")
_settings = get_settings()
_llm = AsyncAnthropic(api_key=_settings.anthropic_api_key)

_DDL = """
CREATE TABLE IF NOT EXISTS yt_comments (
    comment_id   TEXT PRIMARY KEY,
    video_id     TEXT,
    video_title  TEXT,
    author       TEXT,
    author_url   TEXT,
    body         TEXT NOT NULL,
    published_at TIMESTAMPTZ,
    kind         TEXT NOT NULL DEFAULT 'pending',
    draft        TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    reply_text   TEXT,
    reply_id     TEXT,
    handled_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS yt_comments_status_idx ON yt_comments (status, published_at DESC);
CREATE INDEX IF NOT EXISTS yt_comments_kind_idx ON yt_comments (kind, published_at DESC);
"""

_ensured = False

# Cada cuánto revisa el canal. 5 min = 288 llamadas/día = 288 unidades de cuota.
POLL_SECONDS = 300

# Clasificaciones que merecen borrador. El resto se archiva sin gastar tokens.
ANSWERABLE = {"question", "buying"}

# La PRIMERA corrida se encuentra el histórico entero del canal. Clasificar es
# barato (6 tokens) y se hace en paralelo; redactar no lo es, así que se limita
# por corrida y el resto lo va completando el bucle de fondo. Sin esto, el botón
# "Revisar ahora" se quedaría colgado hasta que Render corta la petición.
CLASSIFY_BATCH = 8
MAX_DRAFTS_PER_RUN = 12


async def _ensure(session: AsyncSession) -> None:
    global _ensured
    if _ensured:
        return
    for stmt in filter(None, (s.strip() for s in _DDL.split(";"))):
        await session.execute(text(stmt))
    await session.commit()
    _ensured = True


def _as_dt(value: Any) -> _dt.datetime | None:
    """YouTube manda ISO-8601 con Z. asyncpg no acepta texto para TIMESTAMPTZ."""
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    txt = str(value or "").strip()
    if not txt:
        return None
    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(txt)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_dt.timezone.utc)


# --------------------------------------------------------------------------- #
# Clasificación
# --------------------------------------------------------------------------- #

_SPAM = re.compile(
    r"(crypto|bitcoin|forex|invest|whatsapp\s*\+|t\.me/|telegram|"
    r"promo\s*sm|subscribe to my|sub4sub|check my channel|onlyfans|"
    r"recover.{0,20}(wallet|funds)|hack)",
    re.I,
)

_CLASSIFIER = """You triage comments on an automotive custom-paint YouTube channel.
Reply with ONE word, nothing else:

question - asks something answerable about paint, product, technique, prep, mixing,
           colors, compatibility, shipping, or an order
buying   - shows intent to buy or asks where/how to get a product
praise   - compliment, emoji, hype, "clean!", "fire", generic reaction
spam     - promotion, scam, crypto, self-promo, sub4sub, unrelated links
other    - anything else, including off-topic chatter and insults

If it is a question AND praise, answer question."""


async def classify(body: str) -> str:
    """Una palabra: question / buying / praise / spam / other."""
    stripped = body.strip()
    if not stripped:
        return "other"
    if _SPAM.search(stripped):
        return "spam"
    # Sin letras (solo emojis o signos) nunca es una pregunta.
    if not re.search(r"[a-zA-ZÀ-ɏ]", stripped):
        return "praise"

    model = _settings.yt_classifier_model or _settings.llm_model
    try:
        msg = await _llm.messages.create(
            model=model,
            max_tokens=6,
            system=_CLASSIFIER,
            messages=[{"role": "user", "content": stripped[:1500]}],
        )
        word = "".join(
            b.text for b in msg.content if getattr(b, "type", None) == "text"
        ).strip().lower()
    except Exception:  # noqa: BLE001
        _log.exception("Falló la clasificación; se archiva como 'other'")
        return "other"

    for k in ("question", "buying", "praise", "spam", "other"):
        if k in word:
            return k
    return "other"


# --------------------------------------------------------------------------- #
# Borrador
# --------------------------------------------------------------------------- #

_DRAFT_RULES = """
You are drafting a PUBLIC reply from the Tropical Glitz YouTube channel to a
viewer's comment. A human reviews every draft before it is posted.

Hard rules for this channel — they override the chat formatting guidance:
- Keep it SHORT. Two to four sentences, or a couple of short lines. This is a
  comment box, not a chat window.
- Plain text only. No markdown, no bold, no bullet lists, no headers — YouTube
  renders none of it and asterisks look broken.
- Never paste product links or URLs of any kind. If they need a product, name it
  and tell them it's on tropicalglitz.net.
- Never state a price, a stock level, a discount code or a promotion.
- Never mention an order, tracking or anything account-specific in public. For
  those, reply briefly and send them to support@tropicalglitz.net.
- Answer the actual question first. Warm and direct, no greeting boilerplate,
  no "thanks for watching" filler, no sign-off.
- If the honest answer is that it depends or we don't carry it, say so plainly.
- If you are not confident, keep it to what you're sure of and point them to
  support@tropicalglitz.net rather than guessing.
"""


async def draft_reply(session: AsyncSession, body: str, video_title: str = "") -> str:
    """Genera el borrador con el mismo conocimiento que usa el chat."""
    query = body.strip()[:1500]
    hits = await rag.retrieve(session, query)
    context = rag.build_context(hits)
    # Cerramos la transacción de lectura antes de la llamada al modelo. Si se
    # queda abierta durante los segundos que tarda Claude, Postgres la mata por
    # statement/idle timeout y el INSERT posterior revienta.
    await session.commit()

    prompt = (
        f"{_DRAFT_RULES}\n\n"
        + (f"The comment is on our video: {video_title}\n\n" if video_title else "")
        + f"CONTEXT (our knowledge base — use it, don't invent beyond it):\n{context}\n\n"
        f"VIEWER'S COMMENT:\n{query}\n\n"
        "Write only the reply text."
    )
    msg = await _llm.messages.create(
        model=_settings.llm_model,
        max_tokens=400,
        system=rag.SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    out = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text").strip()
    # Cinturón y tirantes: si se le escapa markdown, lo limpiamos.
    out = re.sub(r"\*\*(.+?)\*\*", r"\1", out)
    out = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", out)
    return out.strip()


# --------------------------------------------------------------------------- #
# Sondeo
# --------------------------------------------------------------------------- #

async def poll_once(
    session: AsyncSession, pages: int = 2, draft_limit: int = MAX_DRAFTS_PER_RUN
) -> dict[str, Any]:
    """Trae comentarios nuevos, los clasifica y redacta los que valen la pena.

    Solo mira hilos de nivel superior. Descarta los que ya respondimos, los
    escritos por el propio canal, y los que ya estaban en la tabla.
    """
    await _ensure(session)
    conn = await youtube.connection(session)
    if not conn:
        return {"ok": False, "error": "YouTube no está conectado."}
    our_channel = conn.get("channel_id") or ""

    seen_new: list[dict[str, Any]] = []
    page_token = ""
    scanned = 0

    for _ in range(max(1, pages)):
        data = await youtube.list_comment_threads(session, page_token=page_token)
        items = data.get("items") or []
        scanned += len(items)
        for it in items:
            top = ((it.get("snippet") or {}).get("topLevelComment") or {})
            sn = top.get("snippet") or {}
            cid = top.get("id") or ""
            if not cid:
                continue

            author_id = ((sn.get("authorChannelId") or {}).get("value")) or ""
            if author_id and author_id == our_channel:
                continue  # comentario nuestro

            # Si ya hay respuestas y alguna es del canal, el hilo está atendido.
            replies = ((it.get("replies") or {}).get("comments")) or []
            if any(
                (((r.get("snippet") or {}).get("authorChannelId") or {}).get("value") == our_channel)
                for r in replies
            ):
                continue

            seen_new.append({
                "comment_id": cid,
                "video_id": (it.get("snippet") or {}).get("videoId") or "",
                "author": sn.get("authorDisplayName") or "",
                "author_url": sn.get("authorChannelUrl") or "",
                "body": sn.get("textOriginal") or sn.get("textDisplay") or "",
                "published_at": _as_dt(sn.get("publishedAt")),
            })

        page_token = data.get("nextPageToken") or ""
        if not page_token:
            break

    if not seen_new:
        return {"ok": True, "scanned": scanned, "nuevos": 0, "borradores": 0}

    # ¿Cuáles no teníamos ya?
    ids = [c["comment_id"] for c in seen_new]
    # bindparam(expanding=True) expande la lista a (:id_1, :id_2, ...). Pasar una
    # lista de Python directo a ANY(:ids) revienta con asyncpg.
    stmt = text("SELECT comment_id FROM yt_comments WHERE comment_id IN :ids").bindparams(
        bindparam("ids", expanding=True)
    )
    known = {r[0] for r in (await session.execute(stmt, {"ids": ids})).all()}
    await session.commit()  # no dejar la transacción abierta durante la clasificación
    fresh = [c for c in seen_new if c["comment_id"] not in known]
    if not fresh:
        return {"ok": True, "scanned": scanned, "nuevos": 0, "borradores": 0}

    titles = await youtube.video_titles(session, [c["video_id"] for c in fresh])
    for c in fresh:
        c["video_title"] = titles.get(c["video_id"], "")

    # Clasificación en paralelo: 200 comentarios de uno en uno tardarían minutos.
    kinds: list[str] = []
    for i in range(0, len(fresh), CLASSIFY_BATCH):
        chunk = fresh[i:i + CLASSIFY_BATCH]
        kinds.extend(await asyncio.gather(*(classify(c["body"]) for c in chunk)))

    # Guardamos YA, sin borrador. Así el trabajo no se pierde si algo falla
    # después, y el panel se puede abrir aunque los borradores lleguen luego.
    for c, kind in zip(fresh, kinds):
        await session.execute(
            text(
                "INSERT INTO yt_comments (comment_id, video_id, video_title, author, author_url,"
                " body, published_at, kind, draft, status) "
                "VALUES (:comment_id, :video_id, :video_title, :author, :author_url, :body,"
                " :published_at, :kind, '', :status) "
                "ON CONFLICT (comment_id) DO NOTHING"
            ),
            {**c, "kind": kind,
             "status": "pending" if kind in ANSWERABLE else "archived"},
        )
    await session.commit()

    drafted = await draft_pending(session, limit=draft_limit) if draft_limit else 0
    faltan = sum(1 for k in kinds if k in ANSWERABLE) - drafted
    return {
        "ok": True, "scanned": scanned, "nuevos": len(fresh),
        "borradores": drafted, "pendientes_de_redactar": max(0, faltan),
    }


async def draft_pending(session: AsyncSession, limit: int = MAX_DRAFTS_PER_RUN) -> int:
    """Redacta los borradores que faltan, de más nuevo a más viejo.

    Se llama al final de cada sondeo y también sola desde el bucle de fondo,
    para ir vaciando la cola sin bloquear una petición del panel.
    """
    rows = (await session.execute(
        text(
            "SELECT comment_id, body, video_title FROM yt_comments "
            "WHERE status = 'pending' AND coalesce(draft, '') = '' "
            "ORDER BY published_at DESC NULLS LAST LIMIT :lim"
        ),
        {"lim": limit},
    )).all()
    await session.commit()  # idem: el bucle de abajo tarda segundos por borrador
    done = 0
    for cid, body, title in rows:
        try:
            draft = await draft_reply(session, body, title or "")
        except Exception:  # noqa: BLE001
            _log.exception("Falló el borrador del comentario %s", cid)
            continue
        await session.execute(
            text("UPDATE yt_comments SET draft = :d WHERE comment_id = :c"),
            {"d": draft, "c": cid},
        )
        await session.commit()   # commit por borrador: el avance no se pierde
        done += 1
    return done


async def mark_replied(session: AsyncSession, comment_id: str, body: str, reply_id: str) -> None:
    await session.execute(
        text(
            "UPDATE yt_comments SET status = 'replied', reply_text = :b, reply_id = :r,"
            " handled_at = now() WHERE comment_id = :c"
        ),
        {"b": body, "r": reply_id, "c": comment_id},
    )
    await session.commit()


async def set_status(session: AsyncSession, comment_id: str, status: str) -> None:
    await session.execute(
        text("UPDATE yt_comments SET status = :s, handled_at = now() WHERE comment_id = :c"),
        {"s": status, "c": comment_id},
    )
    await session.commit()


async def regenerate(session: AsyncSession, comment_id: str) -> str:
    row = (await session.execute(
        text("SELECT body, video_title FROM yt_comments WHERE comment_id = :c"),
        {"c": comment_id},
    )).first()
    if not row:
        return ""
    draft = await draft_reply(session, row[0], row[1] or "")
    await session.execute(
        text("UPDATE yt_comments SET draft = :d WHERE comment_id = :c"),
        {"d": draft, "c": comment_id},
    )
    await session.commit()
    return draft


async def counters(session: AsyncSession) -> dict[str, int]:
    await _ensure(session)
    rows = (await session.execute(
        text("SELECT status, count(*) FROM yt_comments GROUP BY status")
    )).all()
    out = {r[0]: int(r[1]) for r in rows}
    out["total"] = sum(out.values())
    return out


async def listing(session: AsyncSession, status: str = "pending", limit: int = 60) -> list[dict]:
    await _ensure(session)
    sql = (
        "SELECT comment_id, video_id, video_title, author, author_url, body, published_at,"
        " kind, draft, status, reply_text, handled_at FROM yt_comments "
    )
    params: dict[str, Any] = {"lim": limit}
    if status and status != "all":
        sql += "WHERE status = :st "
        params["st"] = status
    sql += "ORDER BY published_at DESC NULLS LAST LIMIT :lim"
    rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Bucle de fondo
# --------------------------------------------------------------------------- #

async def run_forever() -> None:
    """Sondeo continuo. Se lanza en el arranque; si no hay conexión, duerme."""
    await asyncio.sleep(45)  # deja arrancar el resto del backend primero
    while True:
        try:
            async with AsyncSessionLocal() as session:
                if await youtube.connection(session):
                    res = await poll_once(session)
                    if res.get("nuevos"):
                        _log.info("YouTube: %s comentarios nuevos, %s borradores",
                                  res["nuevos"], res.get("borradores", 0))
                    # Cola pendiente del histórico: se va vaciando poco a poco
                    # en vez de intentarlo todo de golpe en la primera corrida.
                    if res.get("pendientes_de_redactar"):
                        await draft_pending(session, limit=MAX_DRAFTS_PER_RUN)
        except Exception:  # noqa: BLE001
            # Nunca dejamos morir el bucle: un fallo de red o de cuota no debe
            # apagar el sondeo hasta el próximo redeploy.
            _log.exception("Fallo en el sondeo de YouTube; se reintenta")
        await asyncio.sleep(POLL_SECONDS)
