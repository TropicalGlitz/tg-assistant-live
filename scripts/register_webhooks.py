"""Registra los webhooks de producto vía Admin API.

Uso:
    python -m scripts.register_webhooks https://tu-backend.com

Requiere SHOPIFY_ADMIN_TOKEN con scope write_products/read_products y la app
instalada en la tienda. Idempotente: Shopify ignora duplicados por (topic,address).
"""
from __future__ import annotations

import asyncio
import sys

import httpx

from app.core.config import get_settings

_settings = get_settings()

TOPICS = {
    "products/create": "/webhooks/shopify/products-create",
    "products/update": "/webhooks/shopify/products-update",
    "products/delete": "/webhooks/shopify/products-delete",
    # Ventas atribuidas al AI: capturar en el momento evita depender del escaneo
    # paginado del historial (que con el volumen real de la tienda subestimaba).
    "orders/create": "/webhooks/shopify/orders-create",
    "orders/updated": "/webhooks/shopify/orders-updated",
}


async def main(base_url: str) -> None:
    api = f"https://{_settings.shopify_shop_domain}/admin/api/{_settings.shopify_api_version}"
    headers = {
        "X-Shopify-Access-Token": _settings.shopify_admin_token,
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        for topic, path in TOPICS.items():
            body = {
                "webhook": {"topic": topic, "address": base_url.rstrip("/") + path, "format": "json"}
            }
            r = await client.post(f"{api}/webhooks.json", json=body)
            print(topic, r.status_code, r.json())


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Uso: python -m scripts.register_webhooks https://tu-backend.com")
    asyncio.run(main(sys.argv[1]))
