"""Cliente Admin GraphQL de Shopify.

Trae TODA la información de cada producto en una sola consulta: descripción,
tipo, tags, opciones, colecciones, variantes (precio/stock/opciones) y — clave —
los METAFIELDS (specs custom: cobertura, ratio de mezcla, compatibilidad, acabado…).
Usado por el backfill y por la hidratación de webhooks.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

from app.core.config import get_settings

_settings = get_settings()

_ENDPOINT = (
    f"https://{_settings.shopify_shop_domain}"
    f"/admin/api/{_settings.shopify_api_version}/graphql.json"
)
_HEADERS = {
    "X-Shopify-Access-Token": _settings.shopify_admin_token,
    "Content-Type": "application/json",
}

# Fragmento con todos los campos que queremos por producto.
_PRODUCT_FIELDS = """
  id
  legacyResourceId
  handle
  title
  descriptionHtml
  productType
  vendor
  tags
  status
  onlineStoreUrl
  totalInventory
  options { name }
  featuredImage { url }
  images(first: 10) { edges { node { url } } }
  collections(first: 25) { edges { node { title } } }
  metafields(first: 100) { edges { node { namespace key value type } } }
  variants(first: 100) {
    edges { node {
      legacyResourceId title sku price compareAtPrice
      availableForSale inventoryQuantity
      selectedOptions { name value }
      metafields(first: 25) { edges { node { namespace key value type } } }
    } }
  }
"""

_QUERY_LIST = f"""
query($cursor: String) {{
  products(first: 50, after: $cursor) {{
    pageInfo {{ hasNextPage endCursor }}
    edges {{ node {{ {_PRODUCT_FIELDS} }} }}
  }}
}}
"""

_QUERY_ONE = f"""
query($id: ID!) {{
  product(id: $id) {{ {_PRODUCT_FIELDS} }}
}}
"""


async def _graphql(client: httpx.AsyncClient, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    r = await client.post(_ENDPOINT, json={"query": query, "variables": variables})
    r.raise_for_status()
    data = r.json()
    if "errors" in data:
        raise RuntimeError(f"Shopify GraphQL error: {data['errors']}")
    return data["data"]


async def fetch_all_products() -> AsyncIterator[dict[str, Any]]:
    """Itera TODOS los productos (paginado por cursor), con metafields incluidos."""
    async with httpx.AsyncClient(headers=_HEADERS, timeout=60) as client:
        cursor = None
        while True:
            data = await _graphql(client, _QUERY_LIST, {"cursor": cursor})
            conn = data["products"]
            for edge in conn["edges"]:
                yield edge["node"]
            if not conn["pageInfo"]["hasNextPage"]:
                break
            cursor = conn["pageInfo"]["endCursor"]


async def fetch_product(product_id: str | int) -> dict[str, Any] | None:
    """Trae un producto completo por id (numérico o gid). Para hidratar webhooks."""
    gid = str(product_id)
    if not gid.startswith("gid://"):
        gid = f"gid://shopify/Product/{gid}"
    async with httpx.AsyncClient(headers=_HEADERS, timeout=30) as client:
        data = await _graphql(client, _QUERY_ONE, {"id": gid})
        return data.get("product")
