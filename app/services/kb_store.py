"""Acceso a las colecciones de conocimiento en pgvector: `faqs` y `kb_chunks`.

Búsqueda por similitud coseno con boost por `time_used` (ranking de utilidad
heredado de REP: las FAQs más recuperadas pesan más).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def _vec(embedding: list[float]) -> str:
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


# ---------- FAQs ----------
async def upsert_faq(
    session: AsyncSession,
    *,
    question: str,
    answer: str,
    synonyms: list[str],
    embedding: list[float],
    recommended_skus: list[str],
    related_product_id: str | None,
    post_action: str,
    time_used: int,
    content_hash: str,
) -> None:
    q = text(
        """
        INSERT INTO faqs (question, answer, synonyms, embedding, recommended_skus,
                          related_product_id, post_action, time_used, content_hash, updated_at)
        VALUES (:q, :a, :syn, CAST(:emb AS vector), :skus, :rel, :act, :used, :hash, now())
        ON CONFLICT ON CONSTRAINT faqs_question_key DO UPDATE SET
            answer=EXCLUDED.answer, embedding=EXCLUDED.embedding,
            recommended_skus=EXCLUDED.recommended_skus, time_used=EXCLUDED.time_used,
            content_hash=EXCLUDED.content_hash, updated_at=now();
        """
    )
    await session.execute(
        q,
        {
            "q": question, "a": answer, "syn": synonyms, "emb": _vec(embedding),
            "skus": recommended_skus, "rel": related_product_id, "act": post_action,
            "used": time_used, "hash": content_hash,
        },
    )
    await session.commit()


async def search_faqs(
    session: AsyncSession, embedding: list[float], top_k: int = 4
) -> list[dict[str, Any]]:
    """Score coseno + pequeño boost logarítmico por time_used."""
    q = text(
        """
        SELECT question, answer, recommended_skus, related_product_id, post_action, time_used,
               (1 - (embedding <=> CAST(:emb AS vector)))
                 + LEAST(0.05, 0.01 * ln(1 + time_used)) AS score
        FROM faqs
        ORDER BY embedding <=> CAST(:emb AS vector)
        LIMIT :k;
        """
    )
    rows = (await session.execute(q, {"emb": _vec(embedding), "k": top_k})).mappings().all()
    return [dict(r) for r in rows]


# ---------- KB chunks (PDFs) ----------
async def upsert_kb_chunk(
    session: AsyncSession, *, doc_name: str, chunk_idx: int, chunk_text: str,
    embedding: list[float], time_used: int, content_hash: str,
) -> None:
    q = text(
        """
        INSERT INTO kb_chunks (doc_name, chunk_idx, text, embedding, time_used, content_hash, updated_at)
        VALUES (:d, :i, :t, CAST(:emb AS vector), :used, :hash, now())
        ON CONFLICT (doc_name, chunk_idx) DO UPDATE SET
            text=EXCLUDED.text, embedding=EXCLUDED.embedding, content_hash=EXCLUDED.content_hash, updated_at=now();
        """
    )
    await session.execute(q, {"d": doc_name, "i": chunk_idx, "t": chunk_text,
                              "emb": _vec(embedding), "used": time_used, "hash": content_hash})
    await session.commit()


async def search_kb(
    session: AsyncSession, embedding: list[float], top_k: int = 3
) -> list[dict[str, Any]]:
    q = text(
        """
        SELECT doc_name, chunk_idx, text,
               1 - (embedding <=> CAST(:emb AS vector)) AS score
        FROM kb_chunks
        ORDER BY embedding <=> CAST(:emb AS vector)
        LIMIT :k;
        """
    )
    rows = (await session.execute(q, {"emb": _vec(embedding), "k": top_k})).mappings().all()
    return [dict(r) for r in rows]
