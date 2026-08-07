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


# ---------------------------------------------------------------------------
# Búsqueda de órdenes para el asistente (herramienta que llama Claude).
# El cliente puede identificar su orden por número, email, teléfono o nombre.
# Por privacidad solo devolvemos estado + envío + tracking (NO dirección/email/tel).
# ---------------------------------------------------------------------------

# Esquema de la herramienta que expone rag.py a Claude (tool-use).
ORDER_TOOL: dict[str, Any] = {
    "name": "lookup_order",
    "description": (
        "Look up a customer's order(s) in the Tropical Glitz store to tell them the "
        "status: whether it has shipped, is still being prepared, and the tracking "
        "number/link. Call this whenever a customer asks about an order, delivery, "
        "shipping or tracking (e.g. 'where is my order'). Prefer the order number "
        "(like 1234). If the customer doesn't know it, you may search by the email used "
        "at checkout, or by phone, or by first and last name. Provide at least one "
        "identifier. Only report what this tool returns — never invent order data."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "order_number": {"type": "string", "description": "Order number, e.g. 1234 or #1234"},
            "email": {"type": "string", "description": "Email used at checkout"},
            "phone": {"type": "string", "description": "Customer phone number"},
            "first_name": {"type": "string"},
            "last_name": {"type": "string"},
        },
    },
}


def _order_summary(o: dict[str, Any]) -> dict[str, Any]:
    """Resumen seguro de una orden para el asistente (sin PII de contacto/dirección)."""
    fs = o.get("fulfillments") or []
    f0 = fs[0] if fs else {}
    tracking = None
    if f0.get("tracking_number"):
        tracking = {
            "company": f0.get("tracking_company"),
            "number": f0.get("tracking_number"),
            "url": f0.get("tracking_url"),
            "shipment_status": f0.get("shipment_status"),  # in_transit, delivered, ...
        }
    items = [
        {"title": li.get("title"), "quantity": li.get("quantity")}
        for li in (o.get("line_items") or [])
    ][:10]
    return {
        "order": o.get("name"),
        "placed_on": (o.get("created_at") or "")[:10],
        "cancelled": bool(o.get("cancelled_at")),
        "payment_status": o.get("financial_status"),
        "fulfillment_status": o.get("fulfillment_status") or "unfulfilled",
        "tracking": tracking,
        "items": items,
    }


async def _get_json(client: httpx.AsyncClient, url: str, params: dict[str, str]) -> dict[str, Any]:
    r = await client.get(url, params=params)
    r.raise_for_status()
    return r.json()


async def search_orders(
    *,
    order_number: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Busca órdenes por número/email (fuertes) o por teléfono/nombre (vía búsqueda de
    clientes -> sus órdenes por email). Devuelve una lista de resúmenes seguros."""
    api = f"https://{_settings.shopify_shop_domain}/admin/api/{_settings.shopify_api_version}"
    headers = {"X-Shopify-Access-Token": _settings.shopify_admin_token}

    async with httpx.AsyncClient(headers=headers, timeout=30) as client:
        # 1) Identificadores fuertes: número de orden y/o email -> orders.json directo.
        if order_number or email:
            params: dict[str, str] = {"status": "any", "limit": str(limit)}
            if order_number:
                on = str(order_number).strip().lstrip("#")
                params["name"] = f"#{on}"
            if email:
                params["email"] = str(email).strip()
            try:
                data = await _get_json(client, f"{api}/orders.json", params)
            except httpx.HTTPStatusError:
                return []
            return [_order_summary(o) for o in data.get("orders", [])][:limit]

        # 2) Identificadores débiles: teléfono/nombre -> buscar cliente(s) y luego sus
        #    órdenes por email (reutiliza la ruta de email, que sí funciona en Orders API).
        terms: list[str] = []
        if phone:
            terms.append(f"phone:{str(phone).strip()}")
        if first_name:
            terms.append(f"first_name:{str(first_name).strip()}")
        if last_name:
            terms.append(f"last_name:{str(last_name).strip()}")
        if not terms:
            return []
        try:
            cdata = await _get_json(
                client, f"{api}/customers/search.json", {"query": " ".join(terms), "limit": "5"}
            )
        except httpx.HTTPStatusError:
            return []
        emails = [c["email"] for c in cdata.get("customers", []) if c.get("email")]
        out: list[dict[str, Any]] = []
        for em in emails[:3]:
            try:
                data = await _get_json(
                    client, f"{api}/orders.json", {"status": "any", "limit": str(limit), "email": em}
                )
            except httpx.HTTPStatusError:
                continue
            out.extend(_order_summary(o) for o in data.get("orders", []))
            if len(out) >= limit:
                break
        return out[:limit]
