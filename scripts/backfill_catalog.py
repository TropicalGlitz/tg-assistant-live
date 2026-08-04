"""Backfill inicial del catálogo Shopify → pgvector (vía GraphQL, con metafields).

Trae TODA la información de cada producto (descripción, tipo, tags, opciones,
colecciones, variantes y metafields/specs) y la ingiere con embeddings en
`product_vectors`. Correr una vez; luego los webhooks mantienen todo sincronizado.

Uso:
    python -m scripts.backfill_catalog
"""
from __future__ import annotations

import asyncio

from app.db.session import AsyncSessionLocal
from app.services import ingest, shopify_admin


async def main() -> None:
    count = 0
    async with AsyncSessionLocal() as session:
        async for node in shopify_admin.fetch_all_products():
            try:
                await ingest.ingest_graphql_product(session, node)
                count += 1
                if count % 25 == 0:
                    print(f"  ingested {count}...")
            except Exception as e:  # noqa: BLE001
                print(f"  ! product {node.get('legacyResourceId')} failed: {e}")
    print(f"Backfill complete: {count} products.")


if __name__ == "__main__":
    asyncio.run(main())
