"""Ingesta de páginas públicas de políticas/FAQ del sitio → tabla `kb_chunks`.

El asistente responde datos store-specific (envíos, devoluciones, tiempos) SOLO
desde el CONTEXTO recuperado. Estas páginas viven en el sitio pero no estaban en
la base de conocimiento, así que el AI no podía responder sobre tiempos de envío.
Aquí las traemos, limpiamos el HTML (quitando nav/header/footer/scripts), troceamos
en ~250 palabras y las embebemos como kb_chunks para que el RAG las recupere.

- `run()` (re)ingiere todas las fuentes (útil tras actualizar una política).
- `run_if_missing()` corre en el arranque solo si aún no están cargadas.
"""
from __future__ import annotations

import html as _html
import logging
import re
from typing import Any

import httpx
from sqlalchemy import text

from app.db.session import AsyncSessionLocal
from app.services import embeddings, kb_store

_log = logging.getLogger("policy_ingest")

# (doc_name, url, time_used) — time_used da un pequeño boost de recuperación.
POLICY_SOURCES: list[tuple[str, str, int]] = [
    ("Shipping Policy", "https://tropicalglitz.net/policies/shipping-policy", 500),
    ("Refund and Return Policy", "https://tropicalglitz.net/policies/refund-policy", 300),
    ("Store FAQs", "https://tropicalglitz.net/pages/faqs", 800),
]

WORDS = 250
OVERLAP = 40

_BLOCK_RE = re.compile(
    r"<(script|style|noscript|svg|head|nav|header|footer|form|iframe)\b[^>]*>.*?</\1>",
    re.I | re.S,
)
_MAIN_RE = re.compile(r"<main\b[^>]*>(.*?)</main>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")


def _extract_main(html_text: str) -> str:
    """Extrae el texto principal de una página HTML, quitando el 'chrome' del tema."""
    m = _MAIN_RE.search(html_text)
    body = m.group(1) if m else html_text
    body = _BLOCK_RE.sub(" ", body)
    txt = _TAG_RE.sub(" ", body)
    txt = _html.unescape(txt)
    return re.sub(r"\s+", " ", txt).strip()


def _chunk(txt: str) -> list[str]:
    words = txt.split(" ")
    if len(words) <= WORDS:
        return [txt] if txt else []
    out: list[str] = []
    i = 0
    while i < len(words):
        out.append(" ".join(words[i : i + WORDS]))
        i += WORDS - OVERLAP
    return out


async def _fetch(url: str) -> str:
    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True, headers={"User-Agent": "TG-Assistant-KB/1.0"}
    ) as client:
        r = await client.get(url)
        r.raise_for_status()
        return r.text


async def run() -> dict[str, Any]:
    """(Re)ingiere todas las fuentes de políticas. Devuelve chunks por documento."""
    results: dict[str, Any] = {}
    async with AsyncSessionLocal() as session:
        for doc, url, used in POLICY_SOURCES:
            try:
                body = _extract_main(await _fetch(url))
                chunks = _chunk(body)
                if not chunks:
                    results[doc] = "no text"
                    continue
                vecs = await embeddings.embed_batch(chunks)
                for idx, (ch, v) in enumerate(zip(chunks, vecs)):
                    await kb_store.upsert_kb_chunk(
                        session,
                        doc_name=doc,
                        chunk_idx=idx,
                        chunk_text=ch,
                        embedding=v,
                        time_used=used,
                        content_hash=embeddings.content_hash(ch),
                    )
                results[doc] = len(chunks)
                _log.info("Ingesta política %s: %s chunk(s)", doc, len(chunks))
            except Exception as e:  # noqa: BLE001
                _log.exception("Fallo al ingerir %s", url)
                results[doc] = f"error: {e}"
    return results


async def _already_loaded() -> bool:
    names = [doc for doc, _, _ in POLICY_SOURCES]
    try:
        async with AsyncSessionLocal() as session:
            row = (
                await session.execute(
                    text("SELECT count(*) FROM kb_chunks WHERE doc_name = ANY(:names)"),
                    {"names": names},
                )
            ).first()
            return bool(row and int(row[0]) > 0)
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo verificar kb_chunks de políticas")
        return False


async def run_if_missing() -> None:
    """Corre la ingesta en el arranque solo si las políticas aún no están cargadas."""
    if await _already_loaded():
        _log.info("Políticas ya cargadas; se omite ingesta de arranque")
        return
    await run()
