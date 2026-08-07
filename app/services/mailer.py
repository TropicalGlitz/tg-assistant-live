"""Envío de email para el handoff a un representante humano (vía Resend).

Cuando un cliente llena el formulario de contacto en el chat, se arma un correo
con sus datos + su pregunta + toda la conversación y se envía a la bandeja de
soporte para que un humano responda. Devuelve (ok, error) — nunca lanza.
"""
from __future__ import annotations

import html as _html
import logging

import httpx

from app.core.config import get_settings

_log = logging.getLogger("mailer")
_settings = get_settings()

_RESEND_URL = "https://api.resend.com/emails"


def _esc(s: str) -> str:
    return _html.escape(s or "")


def _transcript_html(transcript: list[dict[str, str]]) -> str:
    if not transcript:
        return "<p style='color:#888'>(No previous conversation was captured for this visitor.)</p>"
    rows = []
    for m in transcript:
        who = "Customer" if m.get("role") == "user" else "Assistant"
        color = "#ef2c8f" if who == "Customer" else "#3b82f6"
        rows.append(
            f"<p style='margin:6px 0'><b style='color:{color}'>{who}:</b> "
            f"{_esc(m.get('content', ''))}</p>"
        )
    return "".join(rows)


def _transcript_text(transcript: list[dict[str, str]]) -> str:
    if not transcript:
        return "(No previous conversation captured.)"
    out = []
    for m in transcript:
        who = "Customer" if m.get("role") == "user" else "Assistant"
        out.append(f"{who}: {m.get('content', '')}")
    return "\n".join(out)


async def send_contact_email(
    *,
    first_name: str,
    last_name: str,
    phone: str,
    email: str,
    message: str,
    transcript: list[dict[str, str]],
) -> tuple[bool, str | None]:
    """Envía el correo de contacto. Devuelve (True, None) si se envió; si no,
    (False, motivo). No lanza excepciones."""
    if not _settings.resend_api_key:
        return False, "email_not_configured"

    name = (f"{first_name} {last_name}").strip() or "Customer"
    subject = f"New chat contact: {name}"

    html_body = (
        "<div style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#1b1b1f\">"
        "<h2 style='margin:0 0 8px'>New representative request from the chat</h2>"
        "<table style='border-collapse:collapse;font-size:14px;margin-bottom:14px'>"
        f"<tr><td style='padding:2px 10px 2px 0;color:#6b6b74'>Name</td><td><b>{_esc(name)}</b></td></tr>"
        f"<tr><td style='padding:2px 10px 2px 0;color:#6b6b74'>Email</td><td><a href='mailto:{_esc(email)}'>{_esc(email)}</a></td></tr>"
        f"<tr><td style='padding:2px 10px 2px 0;color:#6b6b74'>Phone</td><td>{_esc(phone) or '—'}</td></tr>"
        "</table>"
        "<h3 style='margin:0 0 4px'>What they need help with</h3>"
        f"<p style='white-space:pre-wrap;margin:0 0 16px'>{_esc(message)}</p>"
        "<h3 style='margin:0 0 4px'>Chat conversation</h3>"
        "<div style='background:#f7f7f8;border:1px solid #eee;border-radius:10px;padding:10px 14px'>"
        f"{_transcript_html(transcript)}"
        "</div>"
        "<p style='color:#9a9aa2;font-size:12px;margin-top:16px'>Reply directly to this email to reach the customer.</p>"
        "</div>"
    )

    text_body = (
        f"New representative request from the chat\n\n"
        f"Name: {name}\nEmail: {email}\nPhone: {phone or '-'}\n\n"
        f"What they need help with:\n{message}\n\n"
        f"Chat conversation:\n{_transcript_text(transcript)}\n"
    )

    payload = {
        "from": _settings.contact_from_email,
        "to": [_settings.contact_to_email],
        "reply_to": email or _settings.contact_to_email,
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                _RESEND_URL,
                headers={
                    "Authorization": f"Bearer {_settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if r.status_code >= 400:
            _log.error("Resend error %s: %s", r.status_code, r.text[:500])
            return False, f"send_failed_{r.status_code}"
        return True, None
    except Exception:  # noqa: BLE001
        _log.exception("No se pudo enviar el email de contacto")
        return False, "exception"
