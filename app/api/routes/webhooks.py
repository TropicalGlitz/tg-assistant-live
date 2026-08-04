"""Endpoints de webhooks de Shopify. Validación HMAC obligatoria antes de procesar.

CLAVE: se lee `await request.body()` (raw bytes) y se valida el HMAC contra ESE
buffer, antes de cualquier json.loads. Nunca uses el body ya parseado para el HMAC.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import verify_webhook_hmac
from app.db.session import get_session
from app.services import ingest, vector_store

router = APIRouter(prefix="/webhooks/shopify", tags=["webhooks"])
_settings = get_settings()


async def _verified_body(
    request: Request,
    x_shopify_hmac_sha256: str | None = Header(default=None),
    x_shopify_shop_domain: str | None = Header(default=None),
) -> dict:
    raw = await request.body()
    if not verify_webhook_hmac(raw, x_shopify_hmac_sha256, _settings.shopify_webhook_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid HMAC")
    if x_shopify_shop_domain and x_shopify_shop_domain != _settings.shopify_shop_domain:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown shop")
    return json.loads(raw)


@router.post("/test", status_code=status.HTTP_200_OK)
async def webhook_test(payload: dict = Depends(_verified_body)):
    """Endpoint de prueba: valida el HMAC y hace eco del topic recibido."""
    return {"ok": True, "received_keys": list(payload.keys())[:10]}


@router.post("/products-create", status_code=status.HTTP_200_OK)
@router.post("/products-update", status_code=status.HTTP_200_OK)
async def products_upsert(
    payload: dict = Depends(_verified_body),
    session: AsyncSession = Depends(get_session),
):
    await ingest.ingest_product(session, payload)
    return {"ok": True, "product_id": str(payload.get("id"))}


@router.post("/products-delete", status_code=status.HTTP_200_OK)
async def products_delete(
    payload: dict = Depends(_verified_body),
    session: AsyncSession = Depends(get_session),
):
    await vector_store.delete_product(session, str(payload["id"]))
    return {"ok": True, "deleted": str(payload["id"])}
