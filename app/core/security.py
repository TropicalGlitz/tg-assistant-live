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


def verify_webhook_hmac(raw_body: bytes, hmac_header: str | None, *secrets: str) -> bool:
    """True si la firma del webhook es válida contra ALGUNO de los secretos dados.
    Comparación en tiempo constante.

    Se aceptan varios secretos porque Shopify NO firma todos los webhooks con la
    misma clave: los creados por la Admin API van firmados con el *API secret key*
    de la app, mientras que los creados a mano en Configuración → Notificaciones
    usan el secreto que se muestra en esa pantalla. Probar ambos evita rechazar
    webhooks legítimos por tener configurada la clave "de la otra familia".

    Seguridad: solo se admiten secretos propios de la tienda (los que ya viven en
    las variables de entorno); no se relaja la validación de ninguna otra forma.
    """
    if not hmac_header:
        return False
    for secret in secrets:
        if not secret:
            continue
        digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
        computed = base64.b64encode(digest).decode("utf-8")
        if hmac.compare_digest(computed, hmac_header):
            return True
    return False


def match_webhook_secret(
    raw_body: bytes, hmac_header: str | None, **secrets: str
) -> str | None:
    """Igual que `verify_webhook_hmac`, pero devuelve el NOMBRE del secreto que
    validó la firma (o None si ninguno). Sirve para dejar en el log cuál de las
    claves configuradas es la buena, sin llegar a mostrar su valor.
    """
    if not hmac_header:
        return None
    for name, secret in secrets.items():
        if not secret:
            continue
        digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
        computed = base64.b64encode(digest).decode("utf-8")
        if hmac.compare_digest(computed, hmac_header):
            return name
    return None


def hmac_debug(raw_body: bytes, **secrets: str) -> str:
    """Prefijo de la firma que produce cada secreto configurado, para comparar en
    el log contra la que envió Shopify. NO revela el secreto: la firma HMAC es
    pública (viaja en la cabecera del webhook) y 10 caracteres no son invertibles.
    """
    parts: list[str] = []
    for name, secret in secrets.items():
        if not secret:
            parts.append(f"{name}=(vacío)")
            continue
        digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).digest()
        parts.append(f"{name}={base64.b64encode(digest).decode('utf-8')[:10]}")
    return " ".join(parts)


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
