"""Cliente de la YouTube Data API v3 (OAuth de usuario, canal propio).

Por qué OAuth y no una API key: leer comentarios se puede con key, pero
RESPONDER un comentario actúa en nombre del canal y exige el scope
`youtube.force-ssl`. El refresh token se guarda en la BD (una sola fila) para
que el backend pueda renovar el access token sin intervención humana.

Cuota (10,000 unidades/día por defecto):
  - commentThreads.list ....  1 unidad
  - comments.insert ........ 50 unidades
Con un sondeo cada 5 minutos son 288 unidades/día, así que sobra margen para
unas 190 respuestas diarias.
"""
from __future__ import annotations

import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings

_log = logging.getLogger("youtube")
_settings = get_settings()

API = "https://www.googleapis.com/youtube/v3"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"

_DDL = """
CREATE TABLE IF NOT EXISTS yt_auth (
    id            INT PRIMARY KEY DEFAULT 1,
    refresh_token TEXT NOT NULL,
    channel_id    TEXT,
    channel_title TEXT,
    connected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT yt_auth_single_row CHECK (id = 1)
);
"""

_ensured = False

# Cache en memoria del access token (dura ~1h). Evita pedir uno nuevo en cada
# sondeo; si el proceso reinicia simplemente se vuelve a pedir.
_access: dict[str, Any] = {"token": "", "expires": 0.0}


async def _ensure(session: AsyncSession) -> None:
    global _ensured
    if _ensured:
        return
    for stmt in filter(None, (s.strip() for s in _DDL.split(";"))):
        await session.execute(text(stmt))
    await session.commit()
    _ensured = True


def configured() -> bool:
    """¿Están puestas las credenciales de Google en el entorno?"""
    return bool(_settings.youtube_client_id and _settings.youtube_client_secret)


def auth_url(state: str) -> str:
    """URL de consentimiento de Google.

    `access_type=offline` + `prompt=consent` son obligatorios para que Google
    devuelva un refresh token; sin ellos solo llega un access token de 1 hora y
    el sondeo se cae cuando expira.
    """
    return AUTH_URL + "?" + urlencode({
        "client_id": _settings.youtube_client_id,
        "redirect_uri": _settings.youtube_redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    })


async def connection(session: AsyncSession) -> dict[str, Any] | None:
    await _ensure(session)
    row = (await session.execute(
        text("SELECT channel_id, channel_title, connected_at FROM yt_auth WHERE id = 1")
    )).mappings().first()
    await session.commit()  # lectura corta: no dejar transacción abierta
    return dict(row) if row else None


async def disconnect(session: AsyncSession) -> None:
    await _ensure(session)
    await session.execute(text("DELETE FROM yt_auth WHERE id = 1"))
    await session.commit()
    _access["token"], _access["expires"] = "", 0.0


async def exchange_code(session: AsyncSession, code: str) -> dict[str, Any]:
    """Canjea el `code` del callback por tokens y guarda el refresh token."""
    await _ensure(session)
    async with httpx.AsyncClient(timeout=20) as cli:
        r = await cli.post(TOKEN_URL, data={
            "code": code,
            "client_id": _settings.youtube_client_id,
            "client_secret": _settings.youtube_client_secret,
            "redirect_uri": _settings.youtube_redirect_uri,
            "grant_type": "authorization_code",
        })
    if r.status_code >= 400:
        raise RuntimeError(f"Google rechazó el código ({r.status_code}): {r.text[:300]}")
    tok = r.json()
    refresh = tok.get("refresh_token")
    if not refresh:
        # Pasa cuando la cuenta ya autorizó antes y Google no reemite el refresh.
        raise RuntimeError(
            "Google no devolvió refresh token. Quita el acceso de la app en "
            "myaccount.google.com/permissions y vuelve a conectar."
        )
    _access["token"] = tok.get("access_token", "")
    _access["expires"] = time.time() + float(tok.get("expires_in", 3600)) - 60

    me = await _channel_mine(_access["token"])
    await session.execute(
        text(
            "INSERT INTO yt_auth (id, refresh_token, channel_id, channel_title, connected_at) "
            "VALUES (1, :rt, :cid, :ct, now()) "
            "ON CONFLICT (id) DO UPDATE SET refresh_token = EXCLUDED.refresh_token, "
            "channel_id = EXCLUDED.channel_id, channel_title = EXCLUDED.channel_title, "
            "connected_at = now()"
        ),
        {"rt": refresh, "cid": me.get("id", ""), "ct": me.get("title", "")},
    )
    await session.commit()
    return me


async def _channel_mine(token: str) -> dict[str, str]:
    async with httpx.AsyncClient(timeout=20) as cli:
        r = await cli.get(
            f"{API}/channels",
            params={"part": "id,snippet", "mine": "true"},
            headers={"Authorization": f"Bearer {token}"},
        )
    r.raise_for_status()
    items = r.json().get("items") or []
    if not items:
        raise RuntimeError("La cuenta de Google autorizada no tiene un canal de YouTube.")
    it = items[0]
    return {"id": it.get("id", ""), "title": (it.get("snippet") or {}).get("title", "")}


async def access_token(session: AsyncSession) -> str:
    """Access token vigente, renovándolo con el refresh token si hace falta."""
    if _access["token"] and time.time() < _access["expires"]:
        return _access["token"]

    await _ensure(session)
    row = (await session.execute(
        text("SELECT refresh_token FROM yt_auth WHERE id = 1")
    )).first()
    if not row:
        raise RuntimeError("YouTube no está conectado todavía.")
    await session.commit()  # el refresh con Google tarda; sin esto la tx queda abierta

    async with httpx.AsyncClient(timeout=20) as cli:
        r = await cli.post(TOKEN_URL, data={
            "refresh_token": row[0],
            "client_id": _settings.youtube_client_id,
            "client_secret": _settings.youtube_client_secret,
            "grant_type": "refresh_token",
        })
    if r.status_code >= 400:
        raise RuntimeError(
            "No se pudo renovar el acceso a YouTube. Si la app de Google quedó en modo "
            f"Testing, el permiso caduca a los 7 días y hay que reconectar. ({r.text[:200]})"
        )
    tok = r.json()
    _access["token"] = tok.get("access_token", "")
    _access["expires"] = time.time() + float(tok.get("expires_in", 3600)) - 60
    return _access["token"]


async def list_comment_threads(
    session: AsyncSession, page_token: str = "", max_results: int = 100
) -> dict[str, Any]:
    """Trae hilos de comentarios de TODO el canal, más recientes primero.

    `allThreadsRelatedToChannelId` evita tener que recorrer video por video: una
    sola llamada de 1 unidad cubre el canal entero.
    """
    conn = await connection(session)
    if not conn or not conn.get("channel_id"):
        raise RuntimeError("YouTube no está conectado todavía.")
    token = await access_token(session)
    params = {
        "part": "snippet,replies",
        "allThreadsRelatedToChannelId": conn["channel_id"],
        "order": "time",
        "maxResults": str(max_results),
        "textFormat": "plainText",
    }
    if page_token:
        params["pageToken"] = page_token
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.get(
            f"{API}/commentThreads", params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"YouTube devolvió {r.status_code}: {r.text[:300]}")
    return r.json()


async def video_titles(session: AsyncSession, video_ids: list[str]) -> dict[str, str]:
    """Títulos de video en lote (1 unidad por llamada, hasta 50 ids)."""
    out: dict[str, str] = {}
    ids = [v for v in dict.fromkeys(video_ids) if v]
    if not ids:
        return out
    token = await access_token(session)
    async with httpx.AsyncClient(timeout=30) as cli:
        for i in range(0, len(ids), 50):
            r = await cli.get(
                f"{API}/videos",
                params={"part": "snippet", "id": ",".join(ids[i:i + 50])},
                headers={"Authorization": f"Bearer {token}"},
            )
            if r.status_code >= 400:
                continue
            for it in r.json().get("items") or []:
                out[it.get("id", "")] = (it.get("snippet") or {}).get("title", "")
    return out


async def post_reply(session: AsyncSession, parent_id: str, body: str) -> str:
    """Publica una respuesta dentro del hilo `parent_id`. Devuelve el id creado."""
    token = await access_token(session)
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(
            f"{API}/comments",
            params={"part": "snippet"},
            headers={"Authorization": f"Bearer {token}"},
            json={"snippet": {"parentId": parent_id, "textOriginal": body}},
        )
    if r.status_code >= 400:
        raise RuntimeError(f"YouTube rechazó la respuesta ({r.status_code}): {r.text[:300]}")
    return r.json().get("id", "")
