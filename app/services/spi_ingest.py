"""Hojas técnicas de los clears, primers y accesorios (línea SPI) → `kb_chunks`.

Son los productos que nuestros clientes usan JUNTO con la pintura Tropical Glitz
(clears, primers, sealers, reducers, activadores, adhesion promoter). El asistente
necesita los datos exactos —ratios de mezcla, números de producto, boquillas,
tiempos de flash y recoat— para no inventarlos.

Diseño:
- El texto vive en `data/spi_tech.json`, una sección por producto. Para actualizar
  una hoja técnica se edita ese archivo y se redeploya: la ingesta es idempotente
  por content_hash, así que solo se re-embebe lo que cambió.
- Namespace propio `SPI|<sección>` en kb_chunks, separado de los videos (`YT|`) y
  de las transcripciones (`YTT|`). Entra por `search_kb` como una guía más, y se
  puede actualizar o quitar sin tocar el resto del conocimiento.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.db.session import AsyncSessionLocal
from app.services import embeddings, kb_store

_log = logging.getLogger("spi_ingest")

DOC_PREFIX = "SPI|"
_DATA_FILE = Path(__file__).resolve().parents[2] / "data" / "spi_tech.json"

_CHUNK_SIZE = 1000
_CHUNK_OVERLAP = 160
# Lote chico: el contenedor tiene 512MB y el modelo de embeddings es lo más
# pesado que corre aquí (ver video_ingest para el mismo criterio).
_EMBED_BATCH = 8


def _chunk(text_in: str) -> list[str]:
    """Trocea respetando párrafos: un chunk no debe cortar un ratio de mezcla
    a la mitad. Si un párrafo solo ya excede el tamaño, se parte por palabras."""
    paragraphs = [p.strip() for p in text_in.split("\n") if p.strip()]
    chunks: list[str] = []
    cur: list[str] = []
    size = 0
    for p in paragraphs:
        if size + len(p) > _CHUNK_SIZE and cur:
            chunks.append("\n".join(cur))
            # Solape: arrastra las últimas líneas para no perder el contexto
            keep: list[str] = []
            ks = 0
            for line in reversed(cur):
                ks += len(line)
                if ks > _CHUNK_OVERLAP:
                    break
                keep.append(line)
            cur = list(reversed(keep))
            size = sum(len(x) for x in cur)
        if len(p) > _CHUNK_SIZE:
            words = p.split()
            buf: list[str] = []
            bs = 0
            for w in words:
                buf.append(w)
                bs += len(w) + 1
                if bs >= _CHUNK_SIZE:
                    chunks.append(" ".join(buf))
                    buf, bs = [], 0
            if buf:
                cur.append(" ".join(buf))
                size += bs
        else:
            cur.append(p)
            size += len(p)
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if len(c.strip()) > 60]


def load_sections() -> list[dict[str, str]]:
    try:
        return json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo leer %s", _DATA_FILE)
        return []


async def _existing_hashes() -> dict[str, str]:
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT doc_name || '#' || chunk_idx AS k, content_hash "
                    "FROM kb_chunks WHERE doc_name LIKE :p"
                ),
                {"p": DOC_PREFIX + "%"},
            )
        ).all()
        return {r[0]: r[1] for r in rows}


async def run() -> dict[str, Any]:
    """Ingiere/actualiza las hojas técnicas. Solo embebe lo nuevo o lo editado."""
    sections = load_sections()
    if not sections:
        return {"sections": 0, "ingested": 0, "note": "sin datos"}

    have = await _existing_hashes()

    # (doc_name, idx, texto, hash) de todo lo que debería existir
    wanted: list[tuple[str, int, str, str]] = []
    for sec in sections:
        name = (sec.get("section") or "").strip()
        body = (sec.get("text") or "").strip()
        if not name or not body:
            continue
        doc = DOC_PREFIX + name
        for i, chunk in enumerate(_chunk(body)):
            # El nombre del producto va dentro del chunk: así el modelo siempre
            # sabe de qué producto son el ratio y los tiempos que está leyendo.
            ct = f"[{name}] {chunk}"
            wanted.append((doc, i, ct, embeddings.content_hash(ct)))

    pending = [w for w in wanted if have.get(f"{w[0]}#{w[1]}") != w[3]]
    if not pending:
        _log.info("Hojas técnicas al día (%s chunks); nada que ingerir", len(wanted))
        return {"sections": len(sections), "chunks": len(wanted), "ingested": 0}

    _log.info(
        "Hojas técnicas: %s chunks nuevos/cambiados de %s", len(pending), len(wanted)
    )
    done = 0
    for i in range(0, len(pending), _EMBED_BATCH):
        batch = pending[i : i + _EMBED_BATCH]
        try:
            vecs = await asyncio.wait_for(
                embeddings.embed_batch([b[2] for b in batch]), timeout=180.0
            )
            async with AsyncSessionLocal() as session:
                for (doc, idx, ct, h), vec in zip(batch, vecs):
                    await asyncio.wait_for(
                        kb_store.upsert_kb_chunk(
                            session,
                            doc_name=doc,
                            chunk_idx=idx,
                            chunk_text=ct,
                            embedding=vec,
                            time_used=0,
                            content_hash=h,
                        ),
                        timeout=60.0,
                    )
            done += len(batch)
            _log.info("Hojas técnicas: %s/%s chunks", done, len(pending))
        except Exception:  # noqa: BLE001
            _log.exception("Hojas técnicas: falló el lote %s", i)
    _log.info("Hojas técnicas listas: %s chunks upsertados", done)
    return {"sections": len(sections), "chunks": len(wanted), "ingested": done}


async def run_startup() -> None:
    """Wrapper de arranque: nunca lanza; si falla se reintenta al próximo deploy."""
    try:
        await run()
    except Exception:  # noqa: BLE001
        _log.exception("Falló la ingesta de hojas técnicas")
