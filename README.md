# Shopify RAG Backend

Backend headless que reemplaza a REP.ai: sincroniza el catálogo de Shopify a una
base vectorial (pgvector) vía webhooks y responde en un widget de chat mediante RAG
con Claude.

## Stack

- **API:** FastAPI + Uvicorn (async)
- **Vector DB:** Postgres + `pgvector` (Supabase)
- **Embeddings:** OpenAI `text-embedding-3-small` (1536 dims)
- **LLM:** Anthropic Claude
- **ORM/driver:** SQLAlchemy async + asyncpg

## Arranque local

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # completa tus claves
# 1) crea el esquema en Postgres/Supabase:
psql "$DATABASE_URL" -f sql/001_init_pgvector.sql
# 2) levanta el server:
uvicorn app.main:app --reload --port 8000
```

## Probar la validación de webhook (HMAC) en local

```bash
# expón el server con un túnel (ngrok/cloudflared) para recibir webhooks reales,
# o simula la firma localmente:
BODY='{"id":123,"title":"demo"}'
SECRET='shpss_tu_api_secret'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" -binary | base64)
curl -sX POST http://localhost:8000/webhooks/shopify/test \
  -H "Content-Type: application/json" \
  -H "X-Shopify-Hmac-Sha256: $SIG" \
  -H "X-Shopify-Shop-Domain: mi-tienda.myshopify.com" \
  --data "$BODY"
# -> {"ok":true,"received_keys":["id","title"]}
# firma incorrecta -> 401 Invalid HMAC
```

## Registrar webhooks en Shopify

```bash
python -m scripts.register_webhooks https://tu-backend.com
```

## Estructura

```
app/
  main.py               # FastAPI app + CORS
  core/
    config.py           # settings (env)
    security.py         # verificación HMAC de webhooks
  api/routes/
    webhooks.py         # products/create|update|delete + /test
    chat.py             # endpoint del widget
  schemas/
    product.py          # ProductDocument (doc canónico) + to_embedding_text/to_payload
  services/
    embeddings.py       # OpenAI embeddings
    vector_store.py     # upsert / similarity_search en pgvector
    ingest.py           # Shopify payload -> doc -> embedding -> upsert (idempotente)
    rag.py              # retrieve + generación con Claude
  db/
    session.py          # engine async
sql/001_init_pgvector.sql
scripts/register_webhooks.py
schemas/product_vector.example.json
```
