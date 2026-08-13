"""Endpoints de webhooks de Shopify. Validación HMAC obligatoria antes de procesar.

CLAVE: se lee `await request.body()` (raw bytes) y se valida el HMAC contra ESE
buffer, antes de cualquier json.loads. Nunca uses el body ya parseado para el HMAC.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import hmac_debug, match_webhook_secret
from app.db.session import get_session
from app.services import ai_orders, ingest, vector_store

_log = logging.getLogger("webhooks")
router = APIRouter(prefix="/webhooks/shopify", tags=["webhooks"])
_settings = get_settings()


async def _verified_body(
    request: Request,
    x_shopify_hmac_sha256: str | None = Header(default=None),
    x_shopify_shop_domain: str | None = Header(default=None),
) -> dict:
    raw = await request.body()
    topic = request.headers.get("x-shopify-topic", "?")
    # Se prueban ambos secretos: el dedicado de webhooks y el API secret de la app.
    # Shopify firma con uno u otro según cómo se creó el webhook (API vs. panel).
    matched = match_webhook_secret(
        raw,
        x_shopify_hmac_sha256,
        webhook=_settings.shopify_webhook_secret,
        api=_settings.shopify_api_secret,
    )
    if not matched:
        # Diagnóstico SIN filtrar secretos: solo si la cabecera llegó, el tamaño
        # del body y los primeros caracteres de las firmas (la firma viaja en
        # claro en la cabecera, no es material sensible).
        _log.warning(
            "Webhook RECHAZADO por HMAC (topic=%s) hdr=%s body=%sB recibido=%s calc=%s | "
            "Ninguno de los secretos configurados firma este webhook: pon en Render "
            "SHOPIFY_WEBHOOK_SECRET = API secret key de la app que creó el webhook.",
            topic,
            "sí" if x_shopify_hmac_sha256 else "NO",
            len(raw),
            (x_shopify_hmac_sha256 or "")[:10],
            hmac_debug(
                raw,
                webhook=_settings.shopify_webhook_secret,
                api=_settings.shopify_api_secret,
            ),
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid HMAC")
    if x_shopify_shop_domain and x_shopify_shop_domain != _settings.shopify_shop_domain:
        _log.warning(
            "Webhook RECHAZADO por tienda desconocida: llegó %r, esperábamos %r (topic=%s)",
            x_shopify_shop_domain,
            _settings.shopify_shop_domain,
            topic,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unknown shop")
    _log.info("Webhook OK (topic=%s, firma=%s)", topic, matched)
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


@router.post("/orders-create", status_code=status.HTTP_200_OK)
@router.post("/orders-updated", status_code=status.HTTP_200_OK)
async def orders_upsert(
    payload: dict = Depends(_verified_body),
    session: AsyncSession = Depends(get_session),
):
    """Captura la orden EN EL MOMENTO de la compra si viene del chat.

    Antes el panel escaneaba el historial vía Admin API con tope de 1000 órdenes,
    lo que con el volumen real de la tienda cubría solo ~13 días y subestimaba las
    ventas del AI. Guardando aquí, la métrica es exacta y no depende del tope.

    Las órdenes que NO vienen del chat (la mayoría) se ignoran: solo respondemos
    200 para que Shopify no reintente.
    """
    row = ai_orders.parse_order(payload)
    if not row:
        return {"ok": True, "attributed": False}
    try:
        await ai_orders.upsert(session, row)
    except Exception:  # noqa: BLE001
        # Nunca devolvemos error: si fallamos, Shopify reintenta y además el
        # escaneo por API sigue como respaldo.
        return {"ok": True, "attributed": True, "stored": False}
    return {"ok": True, "attributed": True, "stored": True, "order": row.get("order_name")}
