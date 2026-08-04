"""Utilidades de texto sin dependencias externas (solo stdlib).

Aisladas aquí para poder testear la lógica pura (embeddings local, limpieza de
HTML, hashing) sin arrastrar pydantic/sqlalchemy/openai.
"""
from __future__ import annotations

import hashlib
import math
import re

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9]+")


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def strip_html(html: str | None) -> str:
    if not html:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", html)).strip()


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


def local_embed(text: str, dim: int = 1536) -> list[float]:
    """Embedding local determinista: bag-of-hashed-tokens con signo, L2-normalizado.

    Señal léxica real (solapamiento de palabras → mayor coseno). Solo dev/test.
    """
    vec = [0.0] * dim
    for tok in tokenize(text):
        h = int.from_bytes(hashlib.md5(tok.encode()).digest()[:4], "big")
        idx = h % dim
        sign = 1.0 if (h >> 31) & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]
