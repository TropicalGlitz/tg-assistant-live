"""Motor RAG híbrido: recupera de las 3 colecciones (productos + FAQs + KB),
aplica el umbral de confianza y el escalado a humano, y genera con Claude.

Portado del comportamiento de REP (teardown §5.1, §7.2) pero con grounding real
en catálogo y baja latencia (streaming SSE).
"""
from __future__ import annotations

import json
import re
from typing import Any

from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.system_prompt import (
    CONFIDENCE_THRESHOLD,
    CONTACT,
    ESCALATION_KEYWORDS,
    SYSTEM_PROMPT,
)
from app.services import embeddings, kb_store, orders, vector_store

_settings = get_settings()
_llm = AsyncAnthropic(api_key=_settings.anthropic_api_key)

# Temas que NO se responden con RAG: van a ruta determinista / handoff.
HANDOFF_TOPICS = {"ORDER_STATUS", "CONTACT_DETAILS", "CUSTOMER_SUPPORT", "TRACKING", "CANCELLATION"}

# El feed público devuelve URLs con el dominio myshopify; las mostramos con el
# dominio propio de la tienda para que el enlace sea corto y de marca.
_SHOP_DOMAIN = _settings.shopify_shop_domain
_STORE_DOMAIN = "tropicalglitz.net"


def _pretty_url(url: str | None) -> str | None:
    if url and _SHOP_DOMAIN and _SHOP_DOMAIN in url:
        return url.replace(_SHOP_DOMAIN, _STORE_DOMAIN)
    return url


def _needs_escalation(query: str) -> bool:
    q = query.lower()
    return any(k in q for k in ESCALATION_KEYWORDS)


def _contact_block() -> str:
    return (
        "I'm sorry, I don't have the information you're looking for at the moment. "
        "Please visit the FAQ section on our website for more information, or contact our "
        f"support team for further assistance — email {CONTACT['email']} or call "
        f"{CONTACT['phone']} ({CONTACT['hours']})."
    )


async def handle_order_intent(query: str) -> str | None:
    """Ruta determinista para ORDER_STATUS/TRACKING. Devuelve texto listo o None si no aplica."""
    if not orders.looks_like_order_query(query):
        return None
    m = orders._ORDER_RE.search(query)
    email_m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", query)
    order_no = m.group(1) if m else None
    email = email_m.group(0) if email_m else None
    if not order_no and not email:
        return ("Happy to check your order. Please share your order number (e.g. #1234) "
                "or the email used at checkout.")
    try:
        found = await orders.lookup_order(order_no, email)
    except Exception:  # noqa: BLE001
        found = None
    if found:
        return orders.format_order(found)
    return (f"I couldn't find that order. Double-check the number/email, or reach us at "
            f"{CONTACT['email']} / {CONTACT['phone']}.")


# Palabras de ENVASE/FORMATO en la consulta ("spray can", "aerosol", "12oz"...).
# Casi todas las pinturas tienen variante Spray Can, así que el envase no debe
# sesgar el ranking de productos hacia la línea Drip® (cuyos TÍTULOS contienen
# "Spray Can"): para buscar productos se neutralizan y manda el COLOR/tipo.
# OJO: no se toca el "can" suelto (verbo modal: "can I paint...").
_PACKAGING_RE = re.compile(
    r"\b(?:in\s+(?:a\s+)?)?(?:spray\s*cans?|rattle\s*cans?|aerosols?)\b"
    r"|\bin\s+(?:a\s+)?cans?\b"
    r"|\b12\s*oz\.?\b",
    re.I,
)


async def retrieve(
    session: AsyncSession, query: str, max_price: float | None = None
) -> dict[str, Any]:
    """Recupera de las 3 colecciones y devuelve hits + score máximo."""
    q_vec = await embeddings.embed_text(query)
    # Vector para PRODUCTOS: sin palabras de envase (si quedó algo sustancial).
    p_vec = q_vec
    stripped = re.sub(r"\s+", " ", _PACKAGING_RE.sub(" ", query)).strip(" .,!?")
    if stripped.lower() != query.strip(" .,!?").lower() and len(stripped) >= 3:
        p_vec = await embeddings.embed_text(stripped)
    products = await vector_store.similarity_search(
        session, embedding=p_vec, top_k=_settings.top_k, only_available=True, max_price=max_price
    )
    faqs = await kb_store.search_faqs(session, q_vec, top_k=4)
    kb = await kb_store.search_kb(session, q_vec, top_k=3)
    videos = await kb_store.search_videos(session, q_vec, top_k=2)
    best = max(
        [h["score"] for h in products] + [f["score"] for f in faqs] + [c["score"] for c in kb] + [0.0]
    )
    return {"products": products, "faqs": faqs, "kb": kb, "videos": videos, "best_score": best}


def build_context(hits: dict[str, Any]) -> str:
    blocks: list[str] = []
    for f in hits["faqs"]:
        if f["score"] >= 0.35:
            blocks.append(json.dumps({"type": "faq", "q": f["question"], "a": f["answer"],
                                      "recommends": f["recommended_skus"]}, ensure_ascii=False))
    for c in hits["kb"]:
        if c["score"] >= 0.35:
            blocks.append(json.dumps({"type": "guide", "doc": c["doc_name"],
                                      "text": c["text"][:900]}, ensure_ascii=False))
    # Videos del canal de YouTube: solo si son realmente afines a la pregunta.
    for v in hits.get("videos", []):
        if v["score"] >= 0.5:
            blocks.append(json.dumps({"type": "video", "video": v["text"][:300]},
                                     ensure_ascii=False))
    for p in hits["products"]:
        if p["score"] >= _settings.min_similarity:
            pl = p["payload"]
            blocks.append(json.dumps({
                "type": "product",
                "title": pl["title"],
                "price_min": pl["price_min"], "price_max": pl.get("price_max"),
                "currency": pl["currency"], "available": pl["available"],
                "url": _pretty_url(pl["url"]), "product_type": pl.get("product_type"),
                "collections": pl.get("collections", []),
                "options": [v.get("title") for v in pl.get("variants", [])][:12],
                "specs": pl.get("metafields", {}),   # specs custom del producto
            }, ensure_ascii=False))
    return "\n".join(blocks)


def _user_turn(context: str, query: str, grounded: bool) -> str:
    """Arma el turno del usuario para Claude, indicando qué tan fuerte es el
    grounding del catálogo para que sepa cuándo apoyarse en CONTEXT vs. su
    experiencia general (sin inventar datos de la tienda)."""
    ctx = context.strip() or "(no strong catalog/FAQ match for this question)"
    strength = (
        "STRONG — prefer CONTEXT for the answer and cite the product(s)/FAQ."
        if grounded
        else "WEAK — CONTEXT may not cover this. If it's a general paint/automotive "
        "how-to question, answer from your expertise. Do NOT invent store-specific "
        "facts (prices, stock, policies)."
    )
    return f"CONTEXT (retrieved from store catalog/FAQs/guides):\n{ctx}\n\nGROUNDING: {strength}\n\nCUSTOMER: {query}"


def _order_args(inp: dict[str, Any] | None) -> dict[str, str]:
    """Saneo de los argumentos que Claude pasa a lookup_order: solo claves conocidas
    y valores string no vacíos."""
    inp = inp or {}
    keys = ("order_number", "email", "phone", "first_name", "last_name")
    return {k: str(inp[k]).strip() for k in keys if inp.get(k)}


async def answer_query(
    session: AsyncSession,
    query: str,
    max_price: float | None = None,
    history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if _needs_escalation(query):
        return {"answer": _contact_block(), "handoff": True, "sources": []}

    hits = await retrieve(session, query, max_price=max_price)
    grounded = hits["best_score"] >= CONFIDENCE_THRESHOLD

    # Ya NO cortamos en seco cuando la confianza es baja. En su lugar dejamos que
    # Claude responda: si es una pregunta GENERAL de técnica de pintura/automotriz
    # la contesta desde su experiencia; si es un dato específico de la tienda que no
    # está en CONTEXT, el system prompt le indica no inventar y ofrecer contacto.
    # Incluimos el historial de la sesión para que CONTINÚE la conversación.
    context = build_context(hits)
    messages = list(history or []) + [{"role": "user", "content": _user_turn(context, query, grounded)}]

    # Tool-use: el asistente puede consultar el estado REAL de una orden en Shopify
    # llamando a lookup_order con lo que el cliente le dé (número, email, teléfono o nombre).
    msg = await _llm.messages.create(
        model=_settings.llm_model,
        max_tokens=_settings.llm_max_tokens,
        system=SYSTEM_PROMPT,
        messages=messages,
        tools=[orders.ORDER_TOOL],
    )
    rounds = 0
    while getattr(msg, "stop_reason", None) == "tool_use" and rounds < 3:
        rounds += 1
        messages.append({"role": "assistant", "content": msg.content})
        tool_results: list[dict[str, Any]] = []
        for block in msg.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "lookup_order":
                try:
                    found = await orders.search_orders(**_order_args(block.input))
                    payload: dict[str, Any] = {"orders": found}
                except Exception:  # noqa: BLE001
                    payload = {"error": "lookup_failed"}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(payload, ensure_ascii=False, default=str),
                })
        messages.append({"role": "user", "content": tool_results})
        msg = await _llm.messages.create(
            model=_settings.llm_model,
            max_tokens=_settings.llm_max_tokens,
            system=SYSTEM_PROMPT,
            messages=messages,
            tools=[orders.ORDER_TOOL],
        )
    answer = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    # Solo mostramos tarjetas de los productos que el asistente REALMENTE
    # enlazó en su respuesta (incluye productos de catálogo enlazados desde una FAQ).
    cards = await _cards_for_answer(session, hits, answer)
    return {
        "answer": answer,
        "handoff": False,
        "sources": _sources(hits) if grounded else [],
        "products": cards,
    }


async def answer_query_stream(session: AsyncSession, query: str, max_price: float | None = None):
    """Generador async de tokens para SSE. Baja latencia percibida (lo que le faltaba a REP)."""
    if _needs_escalation(query):
        yield {"type": "message", "text": _contact_block()}
        yield {"type": "done", "handoff": True}
        return

    order_reply = await handle_order_intent(query)
    if order_reply is not None:
        yield {"type": "message", "text": order_reply}
        yield {"type": "done", "handoff": False, "sources": [{"source": "shopify", "ref": "orders"}]}
        return

    hits = await retrieve(session, query, max_price=max_price)
    grounded = hits["best_score"] >= CONFIDENCE_THRESHOLD

    context = build_context(hits)
    async with _llm.messages.stream(
        model=_settings.llm_model,
        max_tokens=_settings.llm_max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _user_turn(context, query, grounded)}],
    ) as stream:
        async for delta in stream.text_stream:
            yield {"type": "token", "text": delta}
    yield {"type": "done", "handoff": False, "sources": _sources(hits) if grounded else []}


def _sources(hits: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for f in hits["faqs"][:3]:
        out.append({"source": "faq", "ref": f["question"], "score": round(f["score"], 3)})
    for c in hits["kb"][:2]:
        out.append({"source": "kb", "ref": c["doc_name"], "score": round(c["score"], 3)})
    for v in hits.get("videos", [])[:2]:
        if v["score"] >= 0.5:
            out.append({"source": "video", "ref": v["text"][:80], "score": round(v["score"], 3)})
    for p in hits["products"][:3]:
        out.append({"source": "product", "ref": p["payload"]["title"], "score": round(p["score"], 3)})
    return out


# Handles de producto enlazados en la respuesta: .../products/<handle>
_PRODUCT_HANDLE_RE = re.compile(r"/products/([^)\s\"'<>]+)")


def _handle_of(url: str | None) -> str:
    if not url:
        return ""
    m = _PRODUCT_HANDLE_RE.search(url)
    return (m.group(1) if m else "").lower()


def _card_from_payload(pl: dict[str, Any]) -> dict[str, Any] | None:
    """Construye una tarjeta de widget desde el payload de un producto (imagen,
    precio y variantes con su ID numérico para la AJAX Cart API del storefront)."""
    variants = [
        {
            "id": v.get("variant_id"),
            "title": v.get("title") or "",
            "price": v.get("price"),
            "available": bool(v.get("available", True)),
        }
        for v in pl.get("variants", [])
        if v.get("variant_id")
    ]
    if not variants:
        return None
    return {
        "title": pl["title"],
        "url": _pretty_url(pl.get("url")),
        "image": pl.get("featured_image"),
        "price_min": pl.get("price_min"),
        "price_max": pl.get("price_max"),
        "currency": pl.get("currency", "USD"),
        "variants": variants,
    }


def _product_cards(hits: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    """Tarjetas de los productos recuperados por similitud (candidatos)."""
    cards: list[dict[str, Any]] = []
    for p in hits["products"][:limit]:
        if p["score"] < _settings.min_similarity:
            continue
        card = _card_from_payload(p["payload"])
        if card:
            cards.append(card)
    return cards


async def _cards_for_answer(
    session: AsyncSession, hits: dict[str, Any], answer: str, limit: int = 3
) -> list[dict[str, Any]]:
    """Muestra una tarjeta (con botón Agregar al carrito) por CADA producto que el
    asistente realmente enlazó en su respuesta. Incluye tanto los productos
    recuperados por similitud como cualquier otro producto del catálogo cuyo enlace
    aparezca en el texto (p. ej. el TG® 2K High Gloss Clear Coat que viene de una FAQ).
    Si la respuesta no enlaza productos, no se muestra ninguna tarjeta."""
    ans = (answer or "").lower()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    # 1) Productos recuperados por similitud que además fueron enlazados/mencionados.
    for c in _product_cards(hits, limit=_settings.top_k):
        url = (c.get("url") or "").lower()
        title = (c.get("title") or "").lower()
        if (url and url in ans) or (title and title in ans):
            key = _handle_of(c.get("url")) or title
            if key not in seen:
                out.append(c)
                seen.add(key)

    # 2) Cualquier producto enlazado por handle que no haya salido en la búsqueda.
    linked = {h.lower() for h in _PRODUCT_HANDLE_RE.findall(answer or "")}
    missing = [h for h in linked if h and h not in seen]
    if missing:
        try:
            payloads = await vector_store.get_products_by_handles(session, missing)
        except Exception:  # noqa: BLE001
            payloads = []
        for pl in payloads:
            card = _card_from_payload(pl)
            if not card:
                continue
            key = _handle_of(card.get("url")) or (card.get("title") or "").lower()
            if key not in seen:
                out.append(card)
                seen.add(key)

    return out[:limit]
