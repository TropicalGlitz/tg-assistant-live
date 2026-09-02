"""Registro automático de los webhooks de órdenes en Shopify.

POR QUÉ EXISTE ESTO. Shopify borra sola una suscripción de webhook creada por
la Admin API después de 8 entregas fallidas seguidas, y cuenta como fallo
cualquier respuesta fuera del rango 200. Durante el periodo en que nuestro
endpoint devolvía 401 por la firma HMAC, Shopify eliminó la suscripción de
`orders/create` — y nadie se enteró: no hay error, simplemente dejan de llegar
avisos y las ventas del chat dejan de registrarse en silencio.

Arreglar el 401 no revive la suscripción: una vez borrada, hay que volver a
crearla. Por eso esto corre en CADA arranque. Es idempotente (Shopify rechaza
duplicados por topic+address) y barato: dos llamadas.
"""
from __future__ import annotations

import logging

import httpx

from app.core.config import get_settings

_log = logging.getLogger("webhook_setup")
_settings = get_settings()

BASE = "https://tg-assistant-ie5p.onrender.com"

TOPICS = {
    "orders/create": "/webhooks/shopify/orders-create",
    "orders/updated": "/webhooks/shopify/orders-updated",
}


async def ensure() -> dict:
    """Crea las suscripciones que falten y deja constancia en el log."""
    if not (_settings.shopify_shop_domain and _settings.shopify_admin_token):
        _log.warning("Sin credenciales de Shopify; no se registran webhooks")
        return {"ok": False}

    api = (
        f"https://{_settings.shopify_shop_domain}"
        f"/admin/api/{_settings.shopify_api_version}"
    )
    headers = {
        "X-Shopify-Access-Token": _settings.shopify_admin_token,
        "Content-Type": "application/json",
    }

    creados, ya_estaban, fallos = [], [], []
    async with httpx.AsyncClient(headers=headers, timeout=30) as cli:
        # Primero miramos qué hay, para no ensuciar el log creando de más.
        activos = set()
        try:
            r = await cli.get(f"{api}/webhooks.json")
            if r.status_code < 400:
                activos = {
                    (w.get("topic"), w.get("address"))
                    for w in r.json().get("webhooks", [])
                }
        except Exception:  # noqa: BLE001
            _log.exception("No se pudo listar los webhooks de Shopify")

        for topic, path in TOPICS.items():
            if (topic, BASE + path) in activos:
                ya_estaban.append(topic)
                continue
            try:
                r = await cli.post(
                    f"{api}/webhooks.json",
                    json={"webhook": {"topic": topic, "address": BASE + path,
                                      "format": "json"}},
                )
                if r.status_code in (200, 201):
                    creados.append(topic)
                elif "already been taken" in r.text:
                    ya_estaban.append(topic)
                else:
                    fallos.append(f"{topic}: {r.status_code} {r.text[:120]}")
            except Exception as e:  # noqa: BLE001
                fallos.append(f"{topic}: {e}")

    if creados:
        # Nivel WARNING a propósito: que una suscripción faltara significa que
        # estuvimos perdiendo ventas, y eso merece verse en los logs.
        _log.warning(
            "Webhooks de Shopify RE-CREADOS: %s. Estaban borrados, así que las "
            "ventas de ese periodo no se capturaron — corre el backfill.",
            ", ".join(creados),
        )
    if fallos:
        _log.error("No se pudieron registrar webhooks: %s", "; ".join(fallos))
    if ya_estaban and not creados and not fallos:
        _log.info("Webhooks de Shopify OK (%s)", ", ".join(ya_estaban))

    return {"ok": not fallos, "creados": creados,
            "ya_estaban": ya_estaban, "fallos": fallos}


async def run_startup() -> None:
    try:
        await ensure()
    except Exception:  # noqa: BLE001
        _log.exception("Falló la verificación de webhooks en el arranque")
