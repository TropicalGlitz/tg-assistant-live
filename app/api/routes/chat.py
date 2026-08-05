"""Endpoints de chat del widget.

Dos vías:
- POST /chat            → respuesta JSON completa (simple).
- GET  /apps/assistant  → App Proxy de Shopify + streaming SSE (producción).
  El widget llama a `https://<tienda>.myshopify.com/apps/assistant?...&signature=...`
  y Shopify reenvía a este backend firmando la petición (verificada abajo).
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Response

from app.core.config import get_settings
from app.core.security import verify_app_proxy_signature
from app.db.session import get_session
from app.services import proactive, rag

router = APIRouter(tags=["chat"])
_settings = get_settings()

# Widget del storefront servido desde el backend: el tema solo carga
# <script src=".../widget.js" defer></script>, así se actualiza sin tocar el tema.
_WIDGET_JS = Path(__file__).resolve().parents[2] / "static" / "widget.js"


@router.get("/widget.js")
async def widget_js() -> FileResponse:
    return FileResponse(
        _WIDGET_JS,
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300"},
    )


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)
    session_id: str | None = None
    max_price: float | None = None


@router.post("/chat")
async def chat(req: ChatRequest, session: AsyncSession = Depends(get_session)):
    return await rag.answer_query(session, req.message, max_price=req.max_price)


@router.get("/apps/assistant")
async def chat_proxy(request: Request, session: AsyncSession = Depends(get_session)):
    """Entrada de producción vía Shopify App Proxy, con streaming SSE."""
    params = dict(request.query_params)
    if not verify_app_proxy_signature(params, _settings.shopify_api_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid proxy signature")

    message = (params.get("message") or "").strip()
    if not message:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Empty message")
    max_price = float(params["max_price"]) if params.get("max_price") else None

    async def event_stream():
        async for chunk in rag.answer_query_stream(session, message, max_price=max_price):
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/apps/proactive")
async def proactive_message(request: Request, session: AsyncSession = Depends(get_session)):
    """Decide qué mensaje proactivo disparar según el contexto del navegador (13 triggers)."""
    params = dict(request.query_params)
    if not verify_app_proxy_signature(params, _settings.shopify_api_secret):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid proxy signature")

    ctx = {
        "page_type": params.get("page_type", "other"),
        "signal": params.get("signal", "idle"),
        "is_returning": params.get("is_returning") == "1",
        "has_purchased": params.get("has_purchased") == "1",
        "in_stock": None if "in_stock" not in params else params.get("in_stock") == "1",
        "product_title": params.get("product_title"),
        "collection": params.get("collection"),
        "cart": [s for s in (params.get("cart") or "").split("|") if s],
        "cart_total": float(params["cart_total"]) if params.get("cart_total") else 0.0,
    }
    result = await proactive.resolve(session, ctx)
    if not result:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return result
