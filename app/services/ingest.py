"""Normalización: payload de Shopify -> ProductDocument -> embedding -> pgvector.

Idempotente: si el `content_hash` del texto de embedding no cambió, se salta la
llamada (cara) al modelo de embeddings y solo se refresca el payload.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.schemas.product import ProductDocument, ProductVariant
from app.services import embeddings, shopify_admin, vector_store

_settings = get_settings()
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(html: str | None) -> str:
    if not html:
        return ""
    return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html)).strip()


def _shop_base_url() -> str:
    return f"https://{_settings.shopify_shop_domain}"


def map_shopify_product(raw: dict[str, Any]) -> ProductDocument:
    """Mapea el JSON del webhook `products/*` (REST Admin) al documento canónico."""
    variants_raw = raw.get("variants", []) or []
    option_names = [o["name"] for o in raw.get("options", []) if o.get("name")]

    variants: list[ProductVariant] = []
    prices: list[float] = []
    total_inventory = 0
    any_available = False
    for v in variants_raw:
        price = float(v.get("price") or 0)
        prices.append(price)
        qty = v.get("inventory_quantity")
        if isinstance(qty, int):
            total_inventory += max(qty, 0)
        available = (v.get("inventory_quantity") or 0) > 0 or v.get("inventory_policy") == "continue"
        any_available = any_available or available
        variants.append(
            ProductVariant(
                variant_id=str(v["id"]),
                title=v.get("title", ""),
                sku=v.get("sku") or None,
                price=price,
                compare_at_price=float(v["compare_at_price"]) if v.get("compare_at_price") else None,
                available=available,
                inventory_quantity=v.get("inventory_quantity"),
                options={
                    name: v.get(f"option{i+1}")
                    for i, name in enumerate(option_names)
                    if v.get(f"option{i+1}")
                },
            )
        )

    handle = raw.get("handle", "")
    images = [img["src"] for img in raw.get("images", []) if img.get("src")]

    return ProductDocument(
        product_id=str(raw["id"]),
        handle=handle,
        url=f"{_shop_base_url()}/products/{handle}",
        title=raw.get("title", ""),
        description=_strip_html(raw.get("body_html")),
        product_type=raw.get("product_type") or None,
        vendor=raw.get("vendor") or None,
        tags=[t.strip() for t in (raw.get("tags") or "").split(",") if t.strip()],
        collections=[],  # se hidrata aparte vía Admin API si se necesita
        options=option_names,
        price_min=min(prices) if prices else 0.0,
        price_max=max(prices) if prices else 0.0,
        available=any_available,
        total_inventory=total_inventory or None,
        variants=variants,
        featured_image=(raw.get("image") or {}).get("src") or (images[0] if images else None),
        images=images,
        status=raw.get("status", "active"),
        shopify_updated_at=_parse_dt(raw.get("updated_at")),
        ingested_at=datetime.now(timezone.utc),
    )


# ---------- Mapeo desde GraphQL (incluye metafields + colecciones) ----------
def _edges(node: dict[str, Any], key: str) -> list[dict[str, Any]]:
    return [e["node"] for e in (node.get(key, {}) or {}).get("edges", [])]


def _clean_metafields(items: list[dict[str, Any]]) -> dict[str, str]:
    """Filtra metafields a valores de texto útiles (descarta referencias/archivos/JSON pesado)."""
    out: dict[str, str] = {}
    for mf in items:
        val = mf.get("value")
        typ = (mf.get("type") or "").lower()
        if not val or "reference" in typ or "file" in typ:
            continue
        if len(str(val)) > 600:  # evita blobs
            continue
        out[f"{mf.get('namespace')}.{mf.get('key')}"] = str(val)
    return out


def map_graphql_product(node: dict[str, Any]) -> ProductDocument:
    option_names = [o["name"] for o in node.get("options", []) if o.get("name")]
    variants: list[ProductVariant] = []
    prices: list[float] = []
    any_available = False
    for v in _edges(node, "variants"):
        price = float(v.get("price") or 0)
        prices.append(price)
        available = bool(v.get("availableForSale"))
        any_available = any_available or available
        opts = {so["name"]: so["value"] for so in v.get("selectedOptions", []) if so.get("value")}
        variants.append(
            ProductVariant(
                variant_id=str(v.get("legacyResourceId")),
                title=v.get("title", ""),
                sku=v.get("sku") or None,
                price=price,
                compare_at_price=float(v["compareAtPrice"]) if v.get("compareAtPrice") else None,
                available=available,
                inventory_quantity=v.get("inventoryQuantity"),
                options=opts,
            )
        )
    handle = node.get("handle", "")
    images = [e["url"] for e in _edges(node, "images") if e.get("url")]
    collections = [c["title"] for c in _edges(node, "collections") if c.get("title")]
    metafields = _clean_metafields(_edges(node, "metafields"))

    return ProductDocument(
        product_id=str(node.get("legacyResourceId")),
        handle=handle,
        url=node.get("onlineStoreUrl") or f"{_shop_base_url()}/products/{handle}",
        title=node.get("title", ""),
        description=_strip_html(node.get("descriptionHtml")),
        product_type=node.get("productType") or None,
        vendor=node.get("vendor") or None,
        tags=node.get("tags", []) if isinstance(node.get("tags"), list) else [],
        collections=collections,
        options=option_names,
        metafields=metafields,
        price_min=min(prices) if prices else 0.0,
        price_max=max(prices) if prices else 0.0,
        available=any_available,
        total_inventory=node.get("totalInventory"),
        variants=variants,
        featured_image=(node.get("featuredImage") or {}).get("url") or (images[0] if images else None),
        images=images,
        status=(node.get("status") or "active").lower(),
        ingested_at=datetime.now(timezone.utc),
    )


async def _persist(session: AsyncSession, doc: ProductDocument) -> bool:
    """Embed (si cambió el contenido) + upsert. Devuelve True si re-embebió."""
    emb_text = doc.to_embedding_text()
    new_hash = embeddings.content_hash(emb_text)
    prev_hash = await vector_store.get_content_hash(session, doc.product_id)
    doc.content_hash = new_hash
    if prev_hash == new_hash:
        # Contenido semántico igual: refresca solo el payload (precio/stock) sin re-embeber.
        # Reutilizamos el vector existente vía UPDATE de payload.
        await vector_store.update_payload(session, doc.product_id, doc.to_payload(), new_hash)
        return False
    vector = await embeddings.embed_text(emb_text)
    await vector_store.upsert_product(session, doc, vector, new_hash)
    return True


async def ingest_graphql_product(session: AsyncSession, node: dict[str, Any]) -> None:
    await _persist(session, map_graphql_product(node))


async def ingest_product(session: AsyncSession, raw: dict[str, Any]) -> None:
    """Entrada de webhook (payload REST). Hidrata con GraphQL para capturar TODA la
    info (metafields, colecciones); si la hidratación falla, usa el payload REST."""
    node = None
    try:
        node = await shopify_admin.fetch_product(raw["id"])
    except Exception:  # noqa: BLE001
        node = None
    doc = map_graphql_product(node) if node else map_shopify_product(raw)
    await _persist(session, doc)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
