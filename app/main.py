"""Punto de entrada FastAPI."""
from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import chat, meta, webhooks, youtube
from app.core.config import get_settings

_settings = get_settings()

# Sin esto, Python deja el root logger en WARNING y TODOS los _log.info() de la
# app se descartan sin dejar rastro. Los usamos para saber qué está pasando en
# los webhooks y en los sondeos, así que tienen que llegar a los logs de Render.
logging.basicConfig(
    level=getattr(logging, _settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    force=True,
)

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
app.include_router(youtube.router)
app.include_router(meta.router)


@app.on_event("startup")
async def _warmup() -> None:
    # Precarga el modelo de embeddings local para que la 1ª petición no espere la carga.
    import asyncio

    from app.services import (
        embeddings,
        kb_seed,
        policy_ingest,
        public_ingest,
        spi_ingest,
        video_ingest,
        webhook_setup,
        yt_comments,
    )

    try:
        await asyncio.to_thread(embeddings._get_model)
    except Exception:  # noqa: BLE001
        pass

    # Tareas de fondo del arranque. IMPORTANTE: guardamos referencias fuertes —
    # sin ellas, asyncio puede recolectar (GC) una tarea a mitad de ejecución y
    # muere en silencio sin log.
    global _bg_tasks
    _bg_tasks = []
    for coro_factory in (
        # Carga inicial del catálogo (feed público, sin token): corre solo si
        # la tabla está incompleta.
        public_ingest.run_if_empty,
        # Siembra/actualiza la base de conocimiento automotriz (idempotente).
        kb_seed.run_seed,
        # Políticas del sitio (envíos, devoluciones, FAQ): solo si faltan.
        policy_ingest.run_if_missing,
        # Hojas técnicas de clears/primers/accesorios: idempotente por hash,
        # se actualiza editando data/spi_tech.json y redeployando.
        spi_ingest.run_startup,
        # Videos del canal de YouTube: idempotente por hash, aprende uploads
        # nuevos en cada arranque.
        video_ingest.run_startup,
        # Bandeja de comentarios de YouTube: sondea el canal cada 5 minutos y
        # deja borradores pendientes de aprobación humana. Si el canal no está
        # conectado, el bucle simplemente duerme.
        yt_comments.run_forever,
        # Shopify BORRA sola una suscripción de webhook tras 8 fallos seguidos.
        # Nos pasó con orders/create y dejamos de capturar ventas sin aviso.
        # Esto la recrea en cada arranque; es idempotente.
        webhook_setup.run_startup,
    ):
        try:
            _bg_tasks.append(asyncio.create_task(coro_factory()))
        except Exception:  # noqa: BLE001
            pass


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
