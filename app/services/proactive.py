"""Motor de proactividad — los 13 "Sales Skills" de REP, portados a reglas server-side.

El widget detecta señales en el navegador (tipo de página, inactividad, exit-intent,
eventos de carrito, nuevo/recurrente) y llama a /apps/proactive. Aquí se decide QUÉ
mensaje disparar. Al vivir en el servidor: controlamos estacionalidad, promos por fecha
(no más "4th of July" en agosto) y grounding real en catálogo.

Prioridad: gana la primera regla que haga match (de la más específica a la más general).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import embeddings, vector_store

# Complementos por tipo de producto (derivado de las FAQs reales de Tropical Glitz).
COMPLEMENTS: list[tuple[tuple[str, ...], list[str]]] = [
    (("candy", "seductive", "kandy"), ["a metallic basecoat (Comet or Galactic Silver)", "clear coat", "reducer"]),
    (("flake",), ["intercoat clear"]),
    (("basecoat", "base coat", "pearl"), ["clear coat"]),
    (("clear",), ["reducer"]),
    (("primer",), ["a basecoat", "clear coat"]),
]
FREE_SHIP = 499


def _complements_for(title: str) -> list[str]:
    t = (title or "").lower()
    for keys, comps in COMPLEMENTS:
        if any(k in t for k in keys):
            return comps
    return ["clear coat"]


async def active_promos(session: AsyncSession) -> list[dict[str, Any]]:
    """Solo promos vigentes por fecha (arregla el bug de la promo vencida de REP)."""
    now = datetime.now(timezone.utc)
    rows = (
        await session.execute(
            text(
                """
                SELECT code, title, description FROM promotions
                WHERE active = true
                  AND (starts_at IS NULL OR starts_at <= :now)
                  AND (ends_at   IS NULL OR ends_at   >= :now)
                ORDER BY ends_at NULLS LAST LIMIT 3;
                """
            ),
            {"now": now},
        )
    ).mappings().all()
    return [dict(r) for r in rows]


async def _one_available_product(session: AsyncSession, query: str) -> dict[str, Any] | None:
    try:
        vec = await embeddings.embed_text(query)
        hits = await vector_store.similarity_search(session, embedding=vec, top_k=1, only_available=True)
        if hits:
            p = hits[0]["payload"]
            return {"title": p["title"], "url": p["url"], "price_min": p["price_min"], "currency": p["currency"]}
    except Exception:  # noqa: BLE001
        return None
    return None


async def resolve(session: AsyncSession, ctx: dict[str, Any]) -> dict[str, Any] | None:
    """ctx: page_type, signal, is_returning, has_purchased, cart(list[str]),
    product_title, in_stock(bool), collection(str), cart_total(float)."""
    page = ctx.get("page_type", "other")
    signal = ctx.get("signal", "idle")
    cart = ctx.get("cart") or []
    promos = await active_promos(session)
    promo_line = f" By the way, {promos[0]['title']} is on right now." if promos else ""

    # 8 — Upsell tras add-to-cart (el más rentable para pintura)
    if signal == "add_to_cart" and cart:
        item = cart[-1]
        comps = _complements_for(item)
        rec = await _one_available_product(session, comps[0])
        msg = f"Great pick on {item}! For a flawless finish most painters also grab {', '.join(comps)}."
        products = [rec] if rec else []
        return {"trigger": "upsell_add_to_cart", "message": msg,
                "chips": ["Add the essentials", "Why do I need these?"], "products": products}

    # 6 — Producto agotado → alternativa disponible
    if page == "product" and ctx.get("in_stock") is False:
        rec = await _one_available_product(session, ctx.get("product_title") or "similar paint")
        msg = "That one's currently out of stock — want a similar color that's available now?"
        return {"trigger": "out_of_stock", "message": msg,
                "chips": ["Show me alternatives", "Notify me when back"], "products": [rec] if rec else []}

    # 9/10 — Carrito visto sin avanzar / abandono
    if page == "cart" or signal in ("cart_view_idle", "abandoned_cart"):
        total = float(ctx.get("cart_total") or 0)
        if total and total < FREE_SHIP:
            msg = f"You're ${FREE_SHIP - total:.2f} away from free US shipping. Want help completing your order?"
        else:
            msg = "Ready to check out? I can help with anything before you complete your order."
        return {"trigger": "cart_nudge", "message": msg + promo_line,
                "chips": ["Go to checkout", "I have a question"], "products": []}

    # 4 — En página de producto, desenganchado
    if page == "product":
        return {"trigger": "product_page", "message": "Have questions about this one? I can tell you how much you'll need, what to pair it with, or show similar colors.",
                "chips": ["How much do I need?", "What do I pair it with?"], "products": []}

    # 5 — En colección, indeciso
    if page == "collection":
        coll = ctx.get("collection") or "this collection"
        return {"trigger": "collection", "message": f"Looking through {coll}? Tell me your project and I'll point you to the best pick.",
                "chips": ["Best sellers", "Help me choose"], "products": []}

    # 11 — Product Finder (cliente busca pero no sabe elegir)
    if signal == "product_finder":
        return {"trigger": "product_finder", "message": "Tell me what you're painting and the look you want, and I'll find the exact products for it.",
                "chips": ["I'm painting a car", "A motorcycle", "Something small"], "products": []}

    # 1/2/3 — Homepage según estado del comprador
    if page == "home":
        if ctx.get("has_purchased"):
            msg = "Welcome back! Need to reorder, or starting a new project?"
        elif ctx.get("is_returning"):
            msg = "Good to see you again! Want me to pick up where you left off or show what's new?"
        else:
            msg = "Welcome to Tropical Glitz! Bold candy paints and metal flakes to make your ride stand out. What are you working on?"
        return {"trigger": "home", "message": msg + promo_line,
                "chips": ["Best sellers", "Help me choose", "Any promotions?"], "products": []}

    # 7 — Convertir desde cualquier otra página
    return {"trigger": "convert_other", "message": "Need a hand finding the right product? Just tell me your project." + promo_line,
            "chips": ["Help me choose", "Best sellers"], "products": []}
