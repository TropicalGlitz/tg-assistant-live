"""Ingesta del canal de YouTube de Tropical Glitz → tabla `kb_chunks`.

El canal tiene cientos de how-to/tips en video. Aquí se descarga el listado
COMPLETO de la playlist de uploads (sin API key, vía el endpoint web público de
YouTube) y se embebe cada título como un chunk `YT|<videoId>` para que el RAG
recupere el video relevante y el asistente pueda enlazarlo en su respuesta.

- Corre al arrancar en segundo plano: idempotente por content_hash, así que solo
  embebe videos NUEVOS o con título cambiado — los uploads futuros se aprenden
  solos en cada deploy/reinicio.
- `run()` también se puede disparar a demanda vía /admin/ingest-videos.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import text

from app.db.session import AsyncSessionLocal
from app.services import embeddings, kb_store

_log = logging.getLogger("video_ingest")

# Playlist "uploads" del canal @tropicalglitz (UC... -> UU...).
UPLOADS_PLAYLIST = "UUO4WKHmbW7ay7hCWr5C8Mow"

_INNERTUBE_URL = "https://www.youtube.com/youtubei/v1/browse"
_CLIENT = {"clientName": "WEB", "clientVersion": "2.20250101.00.00"}
_MAX_PAGES = 40  # tope de seguridad (~4000 videos)

DOC_PREFIX = "YT|"


def _walk(node: Any, key: str, results: list) -> None:
    """Recolecta todos los valores de `key` en un JSON anidado (dict/list)."""
    if isinstance(node, dict):
        if key in node:
            results.append(node[key])
        for v in node.values():
            _walk(v, key, results)
    elif isinstance(node, list):
        for v in node:
            _walk(v, key, results)


def _title_text(t: Any) -> str:
    """Texto de un objeto 'title' de YouTube (formato .content o .runs)."""
    if not isinstance(t, dict):
        return ""
    if t.get("content"):
        return str(t["content"])
    runs = t.get("runs") or []
    return " ".join((run.get("text") or "") for run in runs).strip()


def _extract_items(data: dict) -> list[tuple[str, str]]:
    """(videoId, title) de cada item de la respuesta. Soporta el formato clásico
    (playlistVideoRenderer) y el nuevo (lockupViewModel con contentId)."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    # Formato clásico.
    renderers: list = []
    _walk(data, "playlistVideoRenderer", renderers)
    for r in renderers:
        vid = r.get("videoId")
        title = _title_text(r.get("title"))
        if vid and title and vid not in seen:
            seen.add(vid)
            out.append((vid, " ".join(title.split())))

    # Formato nuevo (UI 2024+): lockupViewModel.
    lockups: list = []
    _walk(data, "lockupViewModel", lockups)
    for l in lockups:
        if l.get("contentType") not in (None, "LOCKUP_CONTENT_TYPE_VIDEO"):
            continue
        vid = l.get("contentId")
        titles: list = []
        _walk(l.get("metadata") or {}, "title", titles)
        title = ""
        for t in titles:
            title = _title_text(t)
            if title:
                break
        if vid and title and vid not in seen:
            seen.add(vid)
            out.append((vid, " ".join(title.split())))
    return out


def _extract_continuation(data: dict) -> str | None:
    tokens: list = []
    _walk(data, "continuationCommand", tokens)
    for t in tokens:
        if isinstance(t, dict) and t.get("token"):
            return t["token"]
    return None


async def fetch_all_videos() -> list[tuple[str, str]]:
    """Pagina la playlist de uploads completa. Devuelve [(videoId, title)]."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    async with httpx.AsyncClient(
        timeout=30.0,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Content-Type": "application/json",
        },
    ) as client:
        body: dict[str, Any] = {
            "context": {"client": _CLIENT},
            "browseId": "VL" + UPLOADS_PLAYLIST,
        }
        for _ in range(_MAX_PAGES):
            resp = await client.post(_INNERTUBE_URL, json=body)
            resp.raise_for_status()
            data = resp.json()
            for vid, title in _extract_items(data):
                if vid not in seen:
                    seen.add(vid)
                    out.append((vid, title))
            token = _extract_continuation(data)
            if not token:
                break
            body = {"context": {"client": _CLIENT}, "continuation": token}
    return out


async def _existing_hashes() -> dict[str, str]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT doc_name, content_hash FROM kb_chunks WHERE doc_name LIKE :p"),
                {"p": DOC_PREFIX + "%"},
            )
        ).all()
        return {r[0]: r[1] for r in rows}


_BATCH = 50  # embebe y upserta por lotes: memoria acotada y progreso persistente


async def ingest_list(videos: list[tuple[str, str]]) -> dict[str, Any]:
    """Upserta una lista [(videoId, title)]: solo lo nuevo/cambiado (por hash)."""
    have = await _existing_hashes()
    pending = [
        (vid, title)
        for vid, title in videos
        if have.get(DOC_PREFIX + vid) != embeddings.content_hash(title)
    ]
    if not pending:
        _log.info("Videos al día (%s recibidos); nada que ingerir", len(videos))
        return {"total": len(videos), "ingested": 0}
    _log.info("Ingesta de videos: %s nuevos/cambiados de %s", len(pending), len(videos))
    done = 0
    for i in range(0, len(pending), _BATCH):
        batch = pending[i : i + _BATCH]
        vecs = await embeddings.embed_batch([t for _, t in batch])
        async with AsyncSessionLocal() as session:
            for (vid, title), vec in zip(batch, vecs):
                await kb_store.upsert_kb_chunk(
                    session,
                    doc_name=DOC_PREFIX + vid,
                    chunk_idx=0,
                    chunk_text=f"{title} — Watch: https://youtu.be/{vid}",
                    embedding=vec,
                    time_used=0,
                    content_hash=embeddings.content_hash(title),
                )
        done += len(batch)
        _log.info("Ingesta de videos: %s/%s", done, len(pending))
    _log.info("Ingesta de videos completa: %s upsertados", done)
    return {"total": len(videos), "ingested": done}


async def ingest_list_safe(videos: list[tuple[str, str]]) -> None:
    """Wrapper para tareas de fondo: nunca lanza, deja el error en el log."""
    try:
        await ingest_list(videos)
    except Exception:  # noqa: BLE001
        _log.exception("Fallo la ingesta de la lista de videos")


async def run() -> dict[str, Any]:
    """Descarga el catálogo del canal y upserta solo lo nuevo/cambiado.

    Nota: desde IPs de datacenter YouTube puede servir solo las primeras páginas
    de la playlist; con eso alcanza para aprender los uploads NUEVOS (siempre
    vienen primero). El histórico profundo se puede cargar una vez vía
    /admin/upload-videos con la lista completa."""
    videos = await fetch_all_videos()
    if not videos:
        return {"total": 0, "ingested": 0, "note": "playlist vacía o formato cambiado"}
    return await ingest_list(videos)


async def run_startup() -> None:
    """Wrapper de arranque: nunca lanza; si YouTube falla, se reintenta en el
    próximo arranque sin afectar el servicio."""
    try:
        await run()
    except Exception:  # noqa: BLE001
        _log.exception("Fallo la ingesta de videos de YouTube")


# ---------------------------------------------------------------------------
# Transcripciones de los videos → chunks `YTT|<videoId>` en kb_chunks.
#
# El navegador (con sesión real de YouTube) extrae por video la URL FIRMADA de
# subtítulos (timedtext) y la manda a /admin/upload-transcripts. El servidor
# valida que la URL sea de youtube.com y del video correcto, descarga el texto,
# lo trocea y lo embebe. Así el AI responde con el CONTENIDO de los videos y
# puede enlazar el video correspondiente.
#
# `YTT|` NO matchea el filtro `LIKE 'YT|%'`, así que estos chunks entran por
# search_kb (guías) y no desplazan la búsqueda de títulos de search_videos.
# ---------------------------------------------------------------------------

import asyncio as _asyncio
from urllib.parse import parse_qs, urlparse

TRANSCRIPT_PREFIX = "YTT|"
_TIMEDTEXT_HOSTS = {"www.youtube.com", "youtube.com"}
_CHUNK_SIZE = 1100
_CHUNK_OVERLAP = 150
_MAX_CHUNKS_PER_VIDEO = 60
_EMBED_BATCH = 32

# Serializa las ingestas de transcripciones: los lotes llegan del navegador más
# rápido de lo que se embebe y sin el lock se apilarían varios hilos de embeddings
# (memoria/CPU) a la vez.
_TRANSCRIPT_LOCK = _asyncio.Lock()


def valid_timedtext_url(url: str, video_id: str) -> bool:
    """Solo aceptamos URLs de subtítulos FIRMADAS por YouTube y del video pedido:
    el contenido queda garantizado por YouTube, no por quien llama al endpoint."""
    try:
        p = urlparse(url)
    except Exception:  # noqa: BLE001
        return False
    if p.scheme != "https" or p.netloc not in _TIMEDTEXT_HOSTS:
        return False
    if p.path != "/api/timedtext":
        return False
    v = (parse_qs(p.query).get("v") or [""])[0]
    return v == video_id


def chunk_transcript(text_in: str) -> list[str]:
    """Trocea en ~1100 chars con solape, cortando en límite de palabra."""
    words = text_in.split()
    if not words:
        return []
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for w in words:
        cur.append(w)
        size += len(w) + 1
        if size >= _CHUNK_SIZE:
            chunks.append(" ".join(cur))
            # Solape: arrastra las últimas palabras para no cortar ideas.
            keep: list[str] = []
            ks = 0
            for kw in reversed(cur):
                ks += len(kw) + 1
                if ks > _CHUNK_OVERLAP:
                    break
                keep.append(kw)
            cur = list(reversed(keep))
            size = sum(len(w) + 1 for w in cur)
            if len(chunks) >= _MAX_CHUNKS_PER_VIDEO:
                return chunks
    if cur and (not chunks or len(" ".join(cur)) > 80):
        chunks.append(" ".join(cur))
    return chunks[:_MAX_CHUNKS_PER_VIDEO]


def _parse_json3(data: dict) -> str:
    """Texto plano de una respuesta timedtext fmt=json3."""
    parts: list[str] = []
    for ev in data.get("events") or []:
        for seg in ev.get("segs") or []:
            t = seg.get("utf8") or ""
            if t and t != "\n":
                parts.append(t)
    text_out = " ".join(parts)
    return " ".join(text_out.split())


async def _fetch_timedtext(client: httpx.AsyncClient, url: str) -> str:
    sep = "&" if "?" in url else "?"
    if "fmt=" not in url:
        url = url + sep + "fmt=json3"
    resp = await client.get(url)
    resp.raise_for_status()
    if not resp.content:
        return ""
    try:
        return _parse_json3(resp.json())
    except ValueError:
        return ""


async def transcribed_ids() -> set[str]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT DISTINCT doc_name FROM kb_chunks WHERE doc_name LIKE :p"),
                {"p": TRANSCRIPT_PREFIX + "%"},
            )
        ).all()
        return {r[0][len(TRANSCRIPT_PREFIX):] for r in rows}


async def catalog_videos() -> list[tuple[str, str]]:
    """[(videoId, title)] del catálogo ya ingerido (chunks YT|)."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text("SELECT doc_name, text FROM kb_chunks WHERE doc_name LIKE :p"),
                {"p": DOC_PREFIX + "%"},
            )
        ).all()
    out: list[tuple[str, str]] = []
    for doc_name, chunk_text in rows:
        vid = doc_name[len(DOC_PREFIX):]
        title = chunk_text.split(" — Watch:")[0].strip()
        out.append((vid, title))
    return out


async def ingest_transcripts(items: list[tuple[str, str, str, str]]) -> dict[str, Any]:
    """Ingesta [(videoId, title, timedtextUrl, fallbackText)]: descarga de
    YouTube (fuente autoritativa), trocea y embebe. Si YouTube limita la IP del
    servidor (respuesta vacía/429), usa el texto de respaldo que mandó el
    navegador — quien de todos modos tuvo que presentar una URL firmada por
    YouTube para ese video. Salta videos ya transcritos (idempotente)."""
    async with _TRANSCRIPT_LOCK:
        have = await transcribed_ids()
        pending = [it for it in items if it[0] not in have]
        if not pending:
            _log.info("Transcripciones: nada nuevo (%s recibidas)", len(items))
            return {"received": len(items), "ingested": 0}
        _log.info("Transcripciones: %s por ingerir", len(pending))
        ok = empty = failed = 0
        async with httpx.AsyncClient(
            timeout=30.0,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
        ) as client:
            for vid, title, url, fallback in pending:
                raw = ""
                try:
                    raw = await _fetch_timedtext(client, url)
                except Exception:  # noqa: BLE001
                    raw = ""
                if not raw and fallback:
                    raw = " ".join(fallback.split())
                await _asyncio.sleep(0.5)
                if not raw:
                    _log.warning("Transcripción %s: descarga vacía/fallida", vid)
                    failed += 1
                    continue
                chunks = chunk_transcript(raw)
                if not chunks:
                    empty += 1
                    continue
                prefix = f"[Video: {title} — https://youtu.be/{vid}] "
                texts = [prefix + c for c in chunks]
                try:
                    for i in range(0, len(texts), _EMBED_BATCH):
                        batch = texts[i : i + _EMBED_BATCH]
                        vecs = await embeddings.embed_batch(batch)
                        async with AsyncSessionLocal() as session:
                            for j, (ct, vec) in enumerate(zip(batch, vecs)):
                                await kb_store.upsert_kb_chunk(
                                    session,
                                    doc_name=TRANSCRIPT_PREFIX + vid,
                                    chunk_idx=i + j,
                                    chunk_text=ct,
                                    embedding=vec,
                                    time_used=0,
                                    content_hash=embeddings.content_hash(ct),
                                )
                    ok += 1
                except Exception:  # noqa: BLE001
                    _log.exception("Transcripción %s: fallo el embedding/upsert", vid)
                    failed += 1
                if ok and ok % 10 == 0:
                    _log.info("Transcripciones: %s/%s videos", ok, len(pending))
        _log.info(
            "Transcripciones listas: %s ok, %s sin subtítulos, %s fallidas", ok, empty, failed
        )
        return {"received": len(items), "ingested": ok, "empty": empty, "failed": failed}


async def ingest_transcripts_safe(items: list[tuple[str, str, str]]) -> None:
    """Wrapper para tareas de fondo: nunca lanza, deja el error en el log."""
    try:
        await ingest_transcripts(items)
    except Exception:  # noqa: BLE001
        _log.exception("Fallo la ingesta de transcripciones")
