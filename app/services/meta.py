"""Cliente de la Graph API de Meta (Instagram + páginas de Facebook).

Solo comentarios por ahora. Los mensajes directos quedan fuera a propósito:
tienen la ventana de 24 horas y datos privados de clientes, y eso se decide
aparte.

Cómo funciona el acceso: el dueño autoriza una vez con Facebook Login; el token
de usuario se cambia por uno de larga duración (~60 días) y con él se piden los
tokens de página, que NO caducan mientras el de usuario siga vivo. Guardamos los
tokens de página, que son los que firman cada respuesta.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings

_log = logging.getLogger("meta")
_settings = get_settings()

GRAPH = "https://graph.facebook.com"
DIALOG = "https://www.facebook.com/{v}/dialog/oauth"

# Permisos mínimos para leer y responder comentarios en IG y en la página.
# NO pedimos nada de mensajería: eso exige otra revisión y otras reglas.
SCOPES = ",".join((
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_engagement",
    "instagram_basic",
    "instagram_manage_comments",
))

_DDL = """
CREATE TABLE IF NOT EXISTS meta_auth (
    id            INT PRIMARY KEY DEFAULT 1,
    user_token    TEXT NOT NULL,
    pages         JSONB NOT NULL DEFAULT '[]'::jsonb,
    connected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT meta_auth_single_row CHECK (id = 1)
);
"""

_ensured = False


def _v() -> str:
    return _settings.meta_api_version


async def _ensure(session: AsyncSession) -> None:
    global _ensured
    if _ensured:
        return
    for stmt in filter(None, (s.strip() for s in _DDL.split(";"))):
        await session.execute(text(stmt))
    await session.commit()
    _ensured = True


def configured() -> bool:
    return bool(_settings.meta_app_id and _settings.meta_app_secret)


def auth_url(state: str) -> str:
    """Diálogo de autorización.

    Las apps de tipo Business usan "Facebook Login for Business", donde los
    permisos NO se piden con `scope` sino con una Configuración creada en el
    panel (`config_id`). Mandar `scope` en ese flujo hace que Facebook conceda
    el login pero SIN activos: por eso /me/accounts volvía vacío.
    """
    params = {
        "client_id": _settings.meta_app_id,
        "redirect_uri": _settings.meta_redirect_uri,
        "state": state,
        "response_type": "code",
    }
    if _settings.meta_login_config_id:
        params["config_id"] = _settings.meta_login_config_id
    else:
        params["scope"] = SCOPES
    return DIALOG.format(v=_v()) + "?" + urlencode(params)


def verify_signature(raw_body: bytes, header: str | None) -> bool:
    """Valida X-Hub-Signature-256 sobre el cuerpo CRUDO.

    Igual que con Shopify: hay que firmar el body sin parsear. Si se json-parsea
    y se re-serializa, la firma nunca coincide.
    """
    if not header or not header.startswith("sha256="):
        return False
    digest = hmac.new(
        _settings.meta_app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(digest, header[7:])


async def connection(session: AsyncSession) -> dict[str, Any] | None:
    await _ensure(session)
    row = (await session.execute(
        text("SELECT pages, connected_at FROM meta_auth WHERE id = 1")
    )).mappings().first()
    await session.commit()
    if not row:
        return None
    pages = row["pages"]
    if isinstance(pages, str):
        pages = json.loads(pages)
    return {"pages": pages or [], "connected_at": row["connected_at"]}


async def disconnect(session: AsyncSession) -> None:
    await _ensure(session)
    await session.execute(text("DELETE FROM meta_auth WHERE id = 1"))
    await session.commit()


async def exchange_code(session: AsyncSession, code: str) -> list[dict[str, Any]]:
    """Canjea el code, consigue token de larga duración y descubre páginas + IG."""
    await _ensure(session)
    async with httpx.AsyncClient(timeout=25) as cli:
        r = await cli.get(f"{GRAPH}/{_v()}/oauth/access_token", params={
            "client_id": _settings.meta_app_id,
            "client_secret": _settings.meta_app_secret,
            "redirect_uri": _settings.meta_redirect_uri,
            "code": code,
        })
        if r.status_code >= 400:
            raise RuntimeError(f"Meta rechazó el código: {r.text[:300]}")
        short = r.json().get("access_token", "")

        # Token de usuario de larga duración (~60 días).
        r = await cli.get(f"{GRAPH}/{_v()}/oauth/access_token", params={
            "grant_type": "fb_exchange_token",
            "client_id": _settings.meta_app_id,
            "client_secret": _settings.meta_app_secret,
            "fb_exchange_token": short,
        })
        if r.status_code >= 400:
            raise RuntimeError(f"No se pudo alargar el token: {r.text[:300]}")
        long_token = r.json().get("access_token", short)

        # Diagnóstico: qué permisos concedió de verdad. Sin esto no hay forma de
        # distinguir "no marcó la página" de "no se pidió el permiso".
        try:
            perm = await cli.get(f"{GRAPH}/{_v()}/me/permissions",
                                 params={"access_token": long_token})
            granted = [d.get("permission") for d in (perm.json().get("data") or [])
                       if d.get("status") == "granted"]
            _log.info("Meta: permisos concedidos = %s", granted)
        except Exception:  # noqa: BLE001
            _log.warning("No se pudieron leer los permisos concedidos")

        # Páginas que administra, con su token propio y su cuenta de IG ligada.
        r = await cli.get(f"{GRAPH}/{_v()}/me/accounts", params={
            "fields": "id,name,access_token,instagram_business_account{id,username}",
            "limit": "100",
            "access_token": long_token,
        })
        if r.status_code >= 400:
            raise RuntimeError(f"No se pudieron leer las páginas: {r.text[:300]}")
        data = r.json().get("data") or []
        _log.info("Meta: /me/accounts devolvió %s página(s): %s",
                  len(data), [d.get("name") for d in data])

    pages = []
    for p in data:
        ig = p.get("instagram_business_account") or {}
        pages.append({
            "page_id": p.get("id", ""),
            "page_name": p.get("name", ""),
            "page_token": p.get("access_token", ""),
            "ig_id": ig.get("id", ""),
            "ig_username": ig.get("username", ""),
        })
    if not pages:
        raise RuntimeError(
            "Facebook devolvió cero páginas. Si la app es de tipo Business, hay "
            "que crear una Configuración en Facebook Login for Business y poner "
            "su id en META_LOGIN_CONFIG_ID; con `scope` suelto el login se "
            "concede pero sin activos. Revisa también ser ADMIN de la página."
        )

    await session.execute(
        text(
            "INSERT INTO meta_auth (id, user_token, pages, connected_at) "
            "VALUES (1, :t, CAST(:p AS JSONB), now()) "
            "ON CONFLICT (id) DO UPDATE SET user_token = EXCLUDED.user_token, "
            "pages = EXCLUDED.pages, connected_at = now()"
        ),
        {"t": long_token, "p": json.dumps(pages)},
    )
    await session.commit()
    return pages


async def _pages(session: AsyncSession) -> list[dict[str, Any]]:
    conn = await connection(session)
    return (conn or {}).get("pages") or []


async def token_for(session: AsyncSession, *, page_id: str = "", ig_id: str = "") -> str:
    """Token de la página dueña del comentario (IG hereda el de su página)."""
    for p in await _pages(session):
        if page_id and p.get("page_id") == page_id:
            return p.get("page_token", "")
        if ig_id and p.get("ig_id") == ig_id:
            return p.get("page_token", "")
    raise RuntimeError("No tengo token para esa página o cuenta de Instagram.")


async def subscribe_pages(session: AsyncSession) -> list[str]:
    """Suscribe la app al campo `feed` de cada página (comentarios de Facebook).

    Instagram se suscribe desde el panel de Webhooks de la app, no por página.
    """
    done = []
    pages = await _pages(session)
    async with httpx.AsyncClient(timeout=25) as cli:
        for p in pages:
            r = await cli.post(
                f"{GRAPH}/{_v()}/{p['page_id']}/subscribed_apps",
                params={
                    "subscribed_fields": "feed",
                    "access_token": p.get("page_token", ""),
                },
            )
            if r.status_code < 400:
                done.append(p.get("page_name") or p["page_id"])
            else:
                _log.warning("No se pudo suscribir la página %s: %s",
                             p.get("page_name"), r.text[:200])
    return done


async def fetch_comment(
    session: AsyncSession, comment_id: str, *, page_id: str = "", ig_id: str = ""
) -> dict[str, Any]:
    """Lee un comentario completo. El webhook a veces llega sin el texto."""
    token = await token_for(session, page_id=page_id, ig_id=ig_id)
    fields = "id,text,message,from,username,timestamp,created_time,parent_id"
    async with httpx.AsyncClient(timeout=25) as cli:
        r = await cli.get(f"{GRAPH}/{_v()}/{comment_id}",
                          params={"fields": fields, "access_token": token})
    if r.status_code >= 400:
        raise RuntimeError(f"No se pudo leer el comentario: {r.text[:300]}")
    return r.json()


async def reply_to_comment(
    session: AsyncSession, comment_id: str, message: str, *,
    page_id: str = "", ig_id: str = "",
) -> str:
    """Publica una respuesta colgando del comentario. Devuelve el id creado."""
    token = await token_for(session, page_id=page_id, ig_id=ig_id)
    async with httpx.AsyncClient(timeout=25) as cli:
        r = await cli.post(
            f"{GRAPH}/{_v()}/{comment_id}/replies" if ig_id
            else f"{GRAPH}/{_v()}/{comment_id}/comments",
            params={"access_token": token},
            data={"message": message},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"Meta rechazó la respuesta ({r.status_code}): {r.text[:300]}")
    return r.json().get("id", "")
