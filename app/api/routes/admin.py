"""Endpoints de administración/bootstrap.

Pensados para entornos sin shell (Render Free). Seguridad por diseño:
- `/admin/ingest-faqs` SOLO corre si la tabla `faqs` está vacía (one-shot,
  se autodesactiva tras la primera carga). El contenido son FAQs públicas.
- `/admin/status` es de solo lectura (conteos para diagnóstico).
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.faq_parse import parse_faq_md
from app.db.session import get_session
from app.services import embeddings, kb_store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])

FAQ_DATA_PATH = "data/rep_faqs_full.md"


@router.get("/status")
async def status_counts(session: AsyncSession = Depends(get_session)):
    out: dict[str, int] = {}
    for table in ("product_vectors", "faqs", "kb_chunks", "conversations"):
        try:
            row = (await session.execute(text(f"SELECT count(*) FROM {table}"))).first()
            out[table] = int(row[0]) if row else 0
        except Exception:
            out[table] = -1  # tabla no existe / error
    return out


@router.post("/ingest-faqs")
async def ingest_faqs(session: AsyncSession = Depends(get_session)):
    """Ingesta reanudable y respetuosa del rate limit del tier gratis de Gemini
    (100 embeddings/min). Trocea de a 40 con pausa, y NO re-embebe las que ya existen
    (idempotente por `question`), así se puede llamar varias veces hasta terminar."""
    existing = {
        r[0]
        for r in (await session.execute(text("SELECT question FROM faqs"))).all()
    }
    faqs = [f for f in parse_faq_md(FAQ_DATA_PATH) if f["question"] not in existing]
    if not faqs:
        total = (await session.execute(text("SELECT count(*) FROM faqs"))).first()[0]
        return {"ingested": 0, "total": int(total), "done": True}

    BATCH, added = 40, 0
    for i in range(0, len(faqs), BATCH):
        chunk = faqs[i : i + BATCH]
        vectors = await embeddings.embed_batch([f["question"] for f in chunk])
        for f, vec in zip(chunk, vectors):
            await kb_store.upsert_faq(
                session,
                question=f["question"],
                answer=f["answer"],
                synonyms=[],
                embedding=vec,
                recommended_skus=f["recommended_skus"],
                related_product_id=None,
                post_action=f["post_action"],
                time_used=f["time_used"],
                content_hash=embeddings.content_hash(f["question"] + "|" + f["answer"]),
            )
            added += 1
        if i + BATCH < len(faqs):
            await asyncio.sleep(30)  # respeta el límite de 100/min del free tier

    total = (await session.execute(text("SELECT count(*) FROM faqs"))).first()[0]
    logger.info("FAQ ingest batch: +%d (total %d)", added, total)
    return {"ingested": added, "total": int(total), "done": int(total) >= 169}
