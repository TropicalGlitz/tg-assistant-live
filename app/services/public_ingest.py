"""Backfill del catálogo desde el FEED PÚBLICO (/products.json) — sin token.

Trae todos los productos publicados (título, descripción, tipo, tags, variantes,
precios, imágenes) paginando /products.json y los ingiere en `product_vectors`.
No incluye metafields (para eso hace falta la Admin API). Pensado para correr como
tarea de fondo al arrancar el backend, una sola vez (si la tabla está vacía).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import text

from app.core.config import get_settings
from app.db.session import AsyncSessionLocal
from app.schemas.product import ProductDocument, ProductVariant
from app.services import ingest

_settings = get_settings()
_log = logging.getLogger("public_ingest")

_PAGE_SIZE = 250


def _shop_base_url() -> str:
    return f"https://{_settings.shopify_shop_domain}"


def map_public_product(raw: dict[str, Any]) -> ProductDocument:
    """Mapea un producto del feed público (/products.json) al documento canónico."""
    option_names = [o["name"] for o in raw.get("options", []) if o.get("name")]
    variants: list[ProductVariant] = []
    prices: list[float] = []
    any_available = False
    for v in raw.get("variants", []) or []:
        try:
            price = float(v.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        prices.append(price)
        available = bool(v.get("available"))
        any_available = any_available or available
        opts: dict[str, str] = {}
        for i, name in enumerate(option_names):
            val = v.get(f"option{i + 1}")
            if val:
                opts[name] = val
        cap = v.get("compare_at_price")
        variants.append(
            ProductVariant(
                variant_id=str(v.get("id")),
                title=v.get("title", "") or "",
                sku=v.get("sku") or None,
                price=price,
                compare_at_price=float(cap) if cap else None,
                available=available,
                inventory_quantity=None,
                options=opts,
            )
        )
    handle = raw.get("handle", "")
    tags = raw.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    elif not isinstance(tags, list):
        tags = []
    images = [img.get("src") for img in raw.get("images", []) if img.get("src")]
    return ProductDocument(
        product_id=str(raw["id"]),
        handle=handle,
        url=f"{_shop_base_url()}/products/{handle}",
        title=raw.get("title", "") or "",
        description=ingest._strip_html(raw.get("body_html")),
        product_type=raw.get("product_type") or None,
        vendor=raw.get("vendor") or None,
        tags=tags,
        collections=[],
        options=option_names,
        metafields={},
        price_min=min(prices) if prices else 0.0,
        price_max=max(prices) if prices else 0.0,
        available=any_available,
        total_inventory=None,
        variants=variants,
        featured_image=images[0] if images else None,
        images=images,
        status="active",
        ingested_at=datetime.now(timezone.utc),
    )


async def fetch_all_public() -> list[dict[str, Any]]:
    """Pagina /products.json hasta agotar el catálogo. Devuelve la lista cruda."""
    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        page = 1
        while True:
            url = f"{_shop_base_url()}/products.json?limit={_PAGE_SIZE}&page={page}"
            resp = await client.get(url)
            resp.raise_for_status()
            products = (resp.json() or {}).get("products", [])
            if not products:
                break
            out.extend(products)
            if len(products) < _PAGE_SIZE:
                break
            page += 1
            if page > 200:  # tope de seguridad (~50k productos)
                break
    return out


async def _count_products() -> int:
    async with AsyncSessionLocal() as session:
        row = (await session.execute(text("SELECT count(*) FROM product_vectors"))).first()
        return int(row[0]) if row else 0


async def run_backfill() -> dict[str, int]:
    """Ingiere todo el catálogo público. Idempotente vía content_hash."""
    raw_products = await fetch_all_public()
    ok = 0
    failed = 0
    async with AsyncSessionLocal() as session:
        for raw in raw_products:
            try:
                await ingest._persist(session, map_public_product(raw))
                ok += 1
            except Exception:  # noqa: BLE001
                failed += 1
                _log.exception("Fallo al ingerir producto %s", raw.get("id"))
    _log.info("Backfill público: %s ok, %s fallidos, %s total", ok, failed, len(raw_products))
    return {"ok": ok, "failed": failed, "total": len(raw_products)}


async def run_if_empty() -> None:
    """Corre el backfill solo si `product_vectors` está vacío (evita re-trabajo por arranque)."""
    try:
        count = await _count_products()
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo consultar product_vectors; se omite backfill")
        return
    if count > 0:
        _log.info("product_vectors ya tiene %s filas; se omite backfill", count)
        return
    _log.info("product_vectors vacío; iniciando backfill del feed público…")
    try:
        await run_backfill()
    except Exception:  # noqa: BLE001
        _log.exception("Backfill público falló")
