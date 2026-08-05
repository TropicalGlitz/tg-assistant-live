"""Punto de entrada FastAPI."""
from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import chat, webhooks
from app.core.config import get_settings

_settings = get_settings()

app = FastAPI(title="Shopify RAG Backend", version="0.1.0")

# El widget corre en el storefront (dominio propio + myshopify) -> CORS abierto.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(webhooks.router)
app.include_router(chat.router)


@app.on_event("startup")
async def _warmup() -> None:
    # Precarga el modelo de embeddings local para que la 1ª petición no espere la carga.
    import asyncio

    from app.services import embeddings, kb_seed, public_ingest

    try:
        await asyncio.to_thread(embeddings._get_model)
    except Exception:  # noqa: BLE001
        pass

    # Carga inicial del catálogo (feed público, sin token) en segundo plano: no
    # bloquea el arranque ni el health check; corre una sola vez si la tabla está vacía.
    try:
        asyncio.create_task(public_ingest.run_if_empty())
    except Exception:  # noqa: BLE001
        pass

    # Siembra la base de conocimiento automotriz/pintura (respuestas generales de
    # técnica) en segundo plano: idempotente, solo inserta lo que falte.
    try:
        asyncio.create_task(kb_seed.run_seed())
    except Exception:  # noqa: BLE001
        pass


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
