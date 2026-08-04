"""Capa de acceso a pgvector: upsert y búsqueda por similitud con filtros.

Se usa SQL directo (no ORM) para poder aprovechar el operador `<=>` (distancia
coseno) de pgvector y los filtros sobre JSONB en la misma consulta.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.product import ProductDocument


def _vec_literal(embedding: list[float]) -> str:
    """pgvector acepta el vector como string '[0.1,0.2,...]'."""
    return "[" + ",".join(f"{x:.8f}" for x in embedding) + "]"


async def upsert_product(
    session: AsyncSession,
    doc: ProductDocument,
    embedding: list[float],
    content_hash: str,
) -> None:
    q = text(
        """
        INSERT INTO product_vectors
            (product_id, embedding, payload, content_hash, updated_at)
        VALUES
            (:product_id, CAST(:embedding AS vector), CAST(:payload AS jsonb), :content_hash, now())
        ON CONFLICT (product_id) DO UPDATE SET
            embedding    = EXCLUDED.embedding,
            payload      = EXCLUDED.payload,
            content_hash = EXCLUDED.content_hash,
            updated_at   = now();
        """
    )
    await session.execute(
        q,
        {
            "product_id": doc.product_id,
            "embedding": _vec_literal(embedding),
            "payload": _dumps(doc.to_payload()),
            "content_hash": content_hash,
        },
    )
    await session.commit()


async def update_payload(
    session: AsyncSession, product_id: str, payload: dict[str, Any], content_hash: str
) -> None:
    """Refresca solo el payload (precio/stock/metafields) reutilizando el embedding existente."""
    await session.execute(
        text(
            """
            UPDATE product_vectors
            SET payload = CAST(:payload AS jsonb), content_hash = :hash, updated_at = now()
            WHERE product_id = :pid;
            """
        ),
        {"pid": product_id, "payload": _dumps(payload), "hash": content_hash},
    )
    await session.commit()


async def delete_product(session: AsyncSession, product_id: str) -> None:
    await session.execute(
        text("DELETE FROM product_vectors WHERE product_id = :pid"), {"pid": product_id}
    )
    await session.commit()


async def get_content_hash(session: AsyncSession, product_id: str) -> str | None:
    row = (
        await session.execute(
            text("SELECT content_hash FROM product_vectors WHERE product_id = :pid"),
            {"pid": product_id},
        )
    ).first()
    return row[0] if row else None


async def similarity_search(
    session: AsyncSession,
    embedding: list[float],
    top_k: int,
    only_available: bool = True,
    max_price: float | None = None,
) -> list[dict[str, Any]]:
    """Devuelve los productos más cercanos. `score` en [0,1] (1 - distancia coseno)."""
    filters = ["1=1"]
    params: dict[str, Any] = {"embedding": _vec_literal(embedding), "k": top_k}
    if only_available:
        filters.append("(payload->>'available')::boolean = true")
    if max_price is not None:
        filters.append("(payload->>'price_min')::numeric <= :max_price")
        params["max_price"] = max_price

    q = text(
        f"""
        SELECT
            product_id,
            payload,
            1 - (embedding <=> CAST(:embedding AS vector)) AS score
        FROM product_vectors
        WHERE {" AND ".join(filters)}
        ORDER BY embedding <=> CAST(:embedding AS vector)
        LIMIT :k;
        """
    )
    rows = (await session.execute(q, params)).mappings().all()
    return [dict(r) for r in rows]


def _dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)
