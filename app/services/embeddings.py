"""Embeddings LOCALES (sin proveedor externo ni API key).

Usa fastembed (ONNX, ligero) con un modelo open-source. Así la única cuenta/clave
externa del sistema es Anthropic (Claude) para la generación. El modelo se descarga
una vez y corre en CPU dentro del backend.

Modelo por defecto: BAAI/bge-small-en-v1.5 (384 dims), buen retrieval en inglés y liviano.
"""
from __future__ import annotations

import asyncio
import hashlib

from fastembed import TextEmbedding

from app.core.config import get_settings

_settings = get_settings()
_model: TextEmbedding | None = None


def _get_model() -> TextEmbedding:
    global _model
    if _model is None:
        _model = TextEmbedding(model_name=_settings.embedding_model)
    return _model


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _embed_sync(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    return [list(map(float, v)) for v in model.embed(texts)]


async def embed_text(text: str) -> list[float]:
    # Corre el modelo en un thread para no bloquear el event loop del servidor.
    vecs = await asyncio.to_thread(_embed_sync, [text])
    return vecs[0]


async def embed_batch(texts: list[str]) -> list[list[float]]:
    return await asyncio.to_thread(_embed_sync, texts)
