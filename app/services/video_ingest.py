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
