"""Modelos de dominio y el DOCUMENTO CANÓNICO que se persiste en pgvector.

Flujo: payload de Shopify -> ProductDocument (normalizado) -> texto para embeddings
-> fila en la tabla `product_vectors` (embedding + payload JSONB).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ProductVariant(BaseModel):
    variant_id: str
    title: str
    sku: str | None = None
    price: float
    compare_at_price: float | None = None
    available: bool = True
    inventory_quantity: int | None = None
    options: dict[str, str] = Field(default_factory=dict)  # {"Talla": "M", "Color": "Negro"}


class ProductDocument(BaseModel):
    """Representación canónica de un producto. Estable frente al esquema de Shopify."""

    # --- Identidad ---
    product_id: str                       # gid o id numérico de Shopify (clave de upsert)
    handle: str
    url: str                              # URL absoluta a la PDP
    title: str

    # --- Contenido semántico (lo que se convierte en embedding) ---
    description: str = ""                 # body_html limpio, sin tags
    product_type: str | None = None
    vendor: str | None = None
    tags: list[str] = Field(default_factory=list)
    collections: list[str] = Field(default_factory=list)
    options: list[str] = Field(default_factory=list)  # nombres de opciones: ["Talla", "Color"]
    # Metafields = TODA la info custom del producto (specs, cobertura, ratio de mezcla,
    # compatibilidad, acabado…). Clave "namespace.key" -> valor en texto.
    metafields: dict[str, str] = Field(default_factory=dict)

    # --- Comercial / filtrable (va al payload, NO al texto de embedding) ---
    price_min: float
    price_max: float
    currency: str = "USD"
    available: bool = True                # ¿hay al menos una variante en stock?
    total_inventory: int | None = None
    variants: list[ProductVariant] = Field(default_factory=list)

    # --- Media ---
    featured_image: str | None = None
    images: list[str] = Field(default_factory=list)

    # --- Metadatos de sincronización ---
    status: str = "active"                # active | draft | archived
    shopify_updated_at: datetime | None = None
    ingested_at: datetime | None = None
    content_hash: str | None = None       # sha256 del texto de embedding: evita re-embeddings inútiles

    def to_embedding_text(self) -> str:
        """Texto denso y natural que se envía al modelo de embeddings.

        Se incluye SOLO señal semántica útil para recuperación. Precio/stock se
        excluyen del texto (cambian mucho y se filtran vía payload), pero el
        rango de precio se deja como pista suave.
        """
        parts: list[str] = [f"Producto: {self.title}"]
        if self.product_type:
            parts.append(f"Tipo: {self.product_type}")
        if self.vendor:
            parts.append(f"Marca: {self.vendor}")
        if self.collections:
            parts.append("Colecciones: " + ", ".join(self.collections))
        if self.tags:
            parts.append("Etiquetas: " + ", ".join(self.tags))
        if self.options:
            parts.append("Opciones disponibles: " + ", ".join(self.options))
        if self.description:
            parts.append(f"Descripción: {self.description}")
        # Specs de metafields: señal de alto valor para responder con precisión.
        if self.metafields:
            specs = "; ".join(f"{k.split('.')[-1]}: {v}" for k, v in self.metafields.items())
            parts.append(f"Especificaciones: {specs}")
        # Variantes con sus opciones (color/tamaño/presentación) para recuperación fina.
        if self.variants:
            vlabels = ", ".join(v.title for v in self.variants[:25] if v.title)
            if vlabels:
                parts.append(f"Presentaciones: {vlabels}")
        return "\n".join(parts)

    def to_payload(self) -> dict[str, Any]:
        """Payload JSONB que se guarda junto al vector. Es lo que el RAG le pasa a Claude
        y sobre lo que se aplican filtros (precio, disponibilidad, colección…)."""
        return {
            "product_id": self.product_id,
            "handle": self.handle,
            "url": self.url,
            "title": self.title,
            "product_type": self.product_type,
            "vendor": self.vendor,
            "tags": self.tags,
            "collections": self.collections,
            "price_min": self.price_min,
            "price_max": self.price_max,
            "currency": self.currency,
            "available": self.available,
            "total_inventory": self.total_inventory,
            "featured_image": self.featured_image,
            "status": self.status,
            "collections": self.collections,
            "metafields": self.metafields,   # disponible para el contexto del LLM
            "variants": [v.model_dump() for v in self.variants],
        }
