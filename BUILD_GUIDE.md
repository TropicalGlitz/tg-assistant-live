# Guía de puesta en marcha — Reemplazo de REP para Tropical Glitz

Backend RAG (FastAPI + pgvector + Claude) + widget (Theme App Extension) que
reemplaza a REP.ai. Comportamiento portado del teardown (`docs/REP_teardown.md`).

## Qué ya está construido

| Pieza | Archivo(s) | Estado |
|-------|-----------|--------|
| System prompt de REP (persona + umbral 0.60 + escalado + contacto) | `app/core/system_prompt.py` | ✅ |
| Retrieval híbrido (catálogo + FAQs + PDFs) + streaming | `app/services/rag.py` | ✅ |
| Stores pgvector (productos / FAQs / KB) | `app/services/vector_store.py`, `kb_store.py` | ✅ |
| Esquema DB (3 colecciones + analytics) | `sql/001_*.sql`, `sql/002_*.sql` | ✅ |
| Webhooks Shopify products/* con HMAC | `app/api/routes/webhooks.py` | ✅ |
| App Proxy firmado + chat SSE | `app/api/routes/chat.py`, `app/core/security.py` | ✅ |
| Importador de las 178 FAQs | `scripts/import_rep_faqs.py` | ✅ |
| Backfill del catálogo | `scripts/backfill_catalog.py` | ✅ |
| Registro de webhooks | `scripts/register_webhooks.py` | ✅ |
| Widget (App Block, Shadow DOM, SSE, accesible) | `extensions/tg-assistant/` | ✅ |
| Docker + compose | `Dockerfile`, `docker-compose.yml` | ✅ |

Pendiente (Fase 3-4 avanzada, cuando esté lo básico corriendo): troceador de los
29 PDFs (necesita los archivos), rutas deterministas de ORDER_STATUS vía Admin API,
y los 13 triggers proactivos completos.

## Puesta en marcha (local)

```bash
cp .env.example .env          # completa las claves (ver abajo)
docker compose up --build     # levanta Postgres+pgvector (corre 001/002) y la API
# en otra terminal, dentro del contenedor api o con el venv:
python -m scripts.import_rep_faqs data/rep_faqs_full.md   # 178 FAQs → pgvector
python -m scripts.backfill_catalog                        # catálogo Shopify → pgvector
```

Prueba el RAG:
```bash
curl -sX POST localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message":"candy red over a silver base for a motorcycle tank, how much do I need?"}'
```

## Estado de la app en Shopify (hecho el 2026-08-03)

> Nota: desde el 1-ene-2026 Shopify ya no permite "legacy custom apps"; se usa el **Dev Dashboard**.

- App creada en Dev Dashboard: **"TG Assistant"** (app id `405939847169`, handle `tg-assistant-1`).
- Versión **`v1-scopes` Active** con scopes `read_products`, `read_orders`.
- Pendiente (necesitan la URL del backend): **App URL**, **App Proxy** (subpath prefix `apps`,
  subpath `assistant`, URL `https://<backend>/apps/assistant`) → se añaden en una versión nueva.
- Pendiente (lo haces tú, es sensible): **instalar** la app en la tienda para generar el
  **Admin API access token**, y copiar **Client ID / Client secret** desde Dev Dashboard → Settings.

## Lo que falta para ir a producción

1. **Hosting del backend** (recomendado: **Render**, Docker nativo). Deploy → obtienes `https://<backend>`.
2. **Supabase**: crear proyecto, correr `sql/001` y `sql/002` en el SQL Editor, copiar la
   connection string (formato asyncpg) → `DATABASE_URL`.
3. **Claves de IA**: `OPENAI_API_KEY` (embeddings) y `ANTHROPIC_API_KEY` (Claude) → `.env`.
4. **Cerrar la app Shopify**: en Dev Dashboard → nueva versión, setear App URL = `https://<backend>`
   y App Proxy → `https://<backend>/apps/assistant`; Release; **instalar** en la tienda;
   copiar Client ID/secret y Admin API token → `.env`.
5. Poblar datos: `python -m scripts.import_rep_faqs data/rep_faqs_full.md` y
   `python -m scripts.backfill_catalog`.
6. Registrar webhooks: `python -m scripts.register_webhooks https://<backend>`.
7. Subir el widget: `shopify app deploy` (Shopify CLI) y activar el App Block "TG Assistant"
   en el editor de temas.

## Diferenciadores vs REP (ya implementados)

- **Grounding real**: responde "qué producto + cuánto" desde catálogo+FAQs (REP se atascaba).
- **Baja latencia**: streaming SSE token a token.
- **Sin promos vencidas**: la lógica de promos vive en tu backend, controlada por fecha.
- **Menos handoffs**: umbral 0.60 + rutas deterministas para los temas donde REP más
  escalaba a humano (contacto, order status, descuentos).
- **Sin lock-in**: sin Voiceflow ni per-turn fee de terceros.
