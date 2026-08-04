"""Ruta determinista de estado de pedido vía Admin API de Shopify.

Los temas ORDER_STATUS / TRACKING / CANCELLATION son donde REP más escalaba a
humano (teardown §7.2: 11+ handoffs c/u). Aquí los resolvemos con datos reales
en vez de alucinar: se busca el pedido por nombre (#1234) o email y se devuelve
estado + tracking. Para acciones sensibles (cancelar/reembolsar) NO se ejecuta;
se informa y se ofrece contacto.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

from app.core.config import get_settings

_settings = get_settings()
_ORDER_RE = re.compile(r"#?(\d{3,})")


def looks_like_order_query(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in ("order status", "my order", "where is my", "tracking", "track my", "#"))


async def lookup_order(order_number: str | None, email: str | None) -> dict[str, Any] | None:
    if not order_number and not email:
        return None
    api = f"https://{_settings.shopify_shop_domain}/admin/api/{_settings.shopify_api_version}"
    headers = {"X-Shopify-Access-Token": _settings.shopify_admin_token}
    params: dict[str, str] = {"status": "any", "limit": "5"}
    if order_number:
        params["name"] = order_number if order_number.startswith("#") else f"#{order_number}"
    if email:
        params["email"] = email
    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        r = await client.get(f"{api}/orders.json", params=params)
        r.raise_for_status()
        orders = r.json().get("orders", [])
    if not orders:
        return None
    o = orders[0]
    fulfillments = o.get("fulfillments", []) or []
    tracking = None
    if fulfillments:
        tracking = {
            "company": fulfillments[0].get("tracking_company"),
            "number": fulfillments[0].get("tracking_number"),
            "url": fulfillments[0].get("tracking_url"),
        }
    return {
        "name": o.get("name"),
        "financial_status": o.get("financial_status"),
        "fulfillment_status": o.get("fulfillment_status") or "unfulfilled",
        "created_at": o.get("created_at"),
        "tracking": tracking,
    }


def format_order(order: dict[str, Any]) -> str:
    t = order.get("tracking")
    base = (
        f"Order {order['name']}: payment {order['financial_status']}, "
        f"fulfillment {order['fulfillment_status']}."
    )
    if t and t.get("number"):
        base += f" Tracking {t.get('company') or ''} {t['number']}."
        if t.get("url"):
            base += f" Track it: {t['url']}"
    return base
