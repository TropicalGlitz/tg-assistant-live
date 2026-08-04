"""Verificación de firmas de Shopify.

Shopify firma cada webhook con HMAC-SHA256 sobre el *cuerpo crudo* (raw body),
usando el `api_secret` de la app como clave, y envía el resultado en base64 en
la cabecera `X-Shopify-Hmac-Sha256`. Hay que validar SIEMPRE contra el body sin
parsear: si json-parseas primero y luego re-serializas, el HMAC no coincide.
"""
from __future__ import annotations

import base64
import hashlib
import hmac


def verify_webhook_hmac(raw_body: bytes, hmac_header: str | None, secret: str) -> bool:
    """Devuelve True si la firma del webhook es válida. Comparación en tiempo constante."""
    if not hmac_header:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
    computed = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(computed, hmac_header)


def verify_app_proxy_signature(params: dict[str, str], secret: str) -> bool:
    """Valida la firma del Shopify App Proxy.

    Shopify firma la petición con HMAC-SHA256 (hex) sobre los query params ordenados
    alfabéticamente y concatenados como `key=value` SIN separadores, excluyendo `signature`.
    Así el widget llega vía `tienda.myshopify.com/apps/<subpath>` heredando la sesión del
    cliente, sin exponer el backend ni abrir CORS.
    """
    signature = params.get("signature")
    if not signature:
        return False
    pairs = [f"{k}={v}" for k, v in sorted(params.items()) if k != "signature"]
    message = "".join(pairs)
    computed = hmac.new(secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)
