-- Migración inicial: extensión pgvector + tabla de vectores de productos.
-- Ejecutar en Supabase (SQL Editor) o Postgres >= 15.

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS product_vectors (
    product_id    TEXT PRIMARY KEY,             -- id de Shopify (clave de upsert)
    embedding     VECTOR(384) NOT NULL,         -- bge-small-en-v1.5 (embeddings locales)
    payload       JSONB NOT NULL,                -- datos filtrables + contexto para el LLM
    content_hash  TEXT NOT NULL,                 -- sha256 del texto embebido (idempotencia)
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Índice ANN para búsqueda por coseno. HNSW: mejor recall/latencia que ivfflat.
CREATE INDEX IF NOT EXISTS idx_product_vectors_embedding
    ON product_vectors
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- Índices para los filtros más comunes del payload.
CREATE INDEX IF NOT EXISTS idx_product_vectors_available
    ON product_vectors (((payload->>'available')::boolean));

CREATE INDEX IF NOT EXISTS idx_product_vectors_price
    ON product_vectors (((payload->>'price_min')::numeric));

CREATE INDEX IF NOT EXISTS idx_product_vectors_payload_gin
    ON product_vectors USING gin (payload jsonb_path_ops);
