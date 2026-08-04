"""Verificación offline del pipeline de Fase 1 usando CÓDIGO REAL de la app
(módulos sin dependencias pesadas). No requiere red, DB ni claves.

Cubre: parseo de FAQs, embeddings locales + ranking de retrieval, y las firmas
de seguridad (webhook HMAC de Shopify + App Proxy). Ejecutar desde la raíz:
    PYTHONPATH=. EMBEDDING_PROVIDER=local python tests/offline_verify.py
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import sys

import numpy as np

from app.core.faq_parse import parse_faq_md
from app.core.security import verify_app_proxy_signature, verify_webhook_hmac
from app.core.textutils import local_embed, strip_html

DIM = 1536
PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, cond):
    results.append((cond, name))
    print(f"[{PASS if cond else FAIL}] {name}")
    return cond


# ---------- 1) Parseo del corpus de FAQs ----------
faqs = parse_faq_md("data/rep_faqs_full.md")
check("FAQ parse: 169 preguntas únicas (178 - 9 duplicadas fusionadas)", len(faqs) == 169)
check("FAQ parse: 33 con productos recomendados", sum(1 for f in faqs if f["recommended_skus"]) == 33)
check("FAQ parse: post_action correcto", all(
    (f["post_action"] == "recommend_product") == bool(f["recommended_skus"]) for f in faqs))
top = max(faqs, key=lambda f: f["time_used"])
check("FAQ parse: la más usada es 'paint a car' (378)", top["time_used"] == 378 and "paint a car" in top["question"])

# ---------- 2) Embeddings locales + ranking de retrieval ----------
v1, v2 = local_embed("how much paint for a car", DIM), local_embed("how much paint for a car", DIM)
check("Embed: determinista (misma entrada → mismo vector)", v1 == v2)
check("Embed: dimensión 1536", len(v1) == DIM)
check("Embed: L2-normalizado (norma ≈ 1)", abs(np.linalg.norm(v1) - 1.0) < 1e-6)

M = np.array([local_embed(f["question"], DIM) for f in faqs], dtype=np.float32)  # (169, 1536)


def retrieve(query, k=3):
    q = np.array(local_embed(query, DIM), dtype=np.float32)
    scores = M @ q  # coseno (vectores ya normalizados)
    idx = np.argsort(-scores)[:k]
    return [(faqs[i]["question"], round(float(scores[i]), 3)) for i in idx]


print("\n--- Demo de retrieval (embeddings locales, señal léxica) ---")
cases = {
    "how much paint do I need for a car": "paint a car",
    "what primer for bare metal": "primer",
    "do you ship to hawaii": "hawaii",
    "what is intercoat clear used for": "intercoat",
}
ok_retrieval = True
for q, expect in cases.items():
    hits = retrieve(q)
    print(f"  Q: {q!r}")
    for h in hits:
        print(f"     -> ({h[1]}) {h[0]}")
    hit_ok = any(expect.lower() in h[0].lower() for h in hits)
    ok_retrieval = ok_retrieval and hit_ok
check("Retrieval: cada consulta recupera una FAQ relevante en el top-3", ok_retrieval)

# ---------- 3) Firmas de seguridad ----------
secret = "shpss_test_secret"
body = b'{"id":123,"title":"demo"}'
good_hmac = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
check("Webhook HMAC: firma válida aceptada", verify_webhook_hmac(body, good_hmac, secret) is True)
check("Webhook HMAC: firma inválida rechazada", verify_webhook_hmac(body, "wrong", secret) is False)
check("Webhook HMAC: sin header rechazado", verify_webhook_hmac(body, None, secret) is False)

params = {"shop": "tropicalglitz.myshopify.com", "path_prefix": "/apps/assistant",
          "timestamp": "1723000000", "message": "candy red over silver"}
msg = "".join(f"{k}={v}" for k, v in sorted(params.items()))
sig = hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
signed = dict(params, signature=sig)
check("App Proxy: firma válida aceptada", verify_app_proxy_signature(signed, secret) is True)
tampered = dict(signed, message="free stuff")
check("App Proxy: parámetro alterado rechazado", verify_app_proxy_signature(tampered, secret) is False)

# ---------- 4) Limpieza de HTML (usada en ingesta de producto/FAQ) ----------
check("strip_html limpia tags y espacios",
      strip_html("<p>Hello&nbsp; <b>world</b></p>\n") == "Hello&nbsp; world")

# ---------- Resumen ----------
passed = sum(1 for c, _ in results if c)
print(f"\n==== {passed}/{len(results)} checks PASSED ====")
sys.exit(0 if passed == len(results) else 1)
