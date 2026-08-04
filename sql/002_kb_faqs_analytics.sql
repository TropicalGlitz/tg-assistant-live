-- Migración 002: FAQs, chunks de KB (PDFs) y analytics.
-- Basado en el teardown de REP: 3 colecciones vectoriales (products + faqs + kb_chunks)
-- + tablas de conversación/eventos para replicar métricas y "Times used".

-- ============ FAQs (las 178 Q&A curadas) ============
CREATE TABLE IF NOT EXISTS faqs (
    id            BIGSERIAL PRIMARY KEY,
    question      TEXT NOT NULL UNIQUE,   -- constraint: faqs_question_key (usada en el upsert)
    answer        TEXT NOT NULL,
    synonyms      TEXT[]  NOT NULL DEFAULT '{}',          -- variantes de la pregunta
    embedding     VECTOR(384) NOT NULL,                   -- sobre question + synonyms
    recommended_skus   TEXT[] NOT NULL DEFAULT '{}',       -- productos a sugerir tras responder
    related_product_id TEXT,                                -- FAQ atada a una PDP
    post_action   TEXT NOT NULL DEFAULT 'offer_assistance' -- offer_assistance|recommend_product|recommend_collection|continue_flow
                  CHECK (post_action IN ('offer_assistance','recommend_product','recommend_collection','continue_flow')),
    time_used     INT  NOT NULL DEFAULT 0,                  -- ranking de utilidad (boost de retrieval)
    source        TEXT NOT NULL DEFAULT 'rep_import',
    content_hash  TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_faqs_embedding
    ON faqs USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_faqs_time_used ON faqs (time_used DESC);

-- ============ KB chunks (los 29 PDFs troceados) ============
CREATE TABLE IF NOT EXISTS kb_chunks (
    id            BIGSERIAL PRIMARY KEY,
    doc_name      TEXT NOT NULL,                            -- p.ej. "Recommended Paint Quantity Based on Project Size.pdf"
    chunk_idx     INT  NOT NULL,
    text          TEXT NOT NULL,
    embedding     VECTOR(384) NOT NULL,
    time_used     INT  NOT NULL DEFAULT 0,
    content_hash  TEXT NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (doc_name, chunk_idx)
);

CREATE INDEX IF NOT EXISTS idx_kb_chunks_embedding
    ON kb_chunks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- ============ Analytics: conversaciones, mensajes, eventos ============
CREATE TABLE IF NOT EXISTS conversations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    TEXT,
    initiated_by  TEXT CHECK (initiated_by IN ('shopper','ai')),   -- proactivo vs shopper
    topic         TEXT,                                             -- taxonomía §7.3 del teardown
    resolved_by_ai BOOLEAN,
    human_handoff BOOLEAN NOT NULL DEFAULT false,
    location      TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at      TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS messages (
    id            BIGSERIAL PRIMARY KEY,
    conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
    role          TEXT NOT NULL CHECK (role IN ('user','assistant','system')),
    content       TEXT NOT NULL,
    -- citas del RAG: qué se recuperó para esta respuesta (estilo "Sources"/"Show reasoning" de REP)
    retrieved     JSONB,      -- [{source:'faq'|'product'|'kb', id, score}]
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Eventos de comportamiento (para triggers proactivos y tags de sesión §7.3)
CREATE TABLE IF NOT EXISTS events (
    id            BIGSERIAL PRIMARY KEY,
    conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
    type          TEXT NOT NULL,   -- page_view | cart_view | add_to_cart | product_view | fallback | lead_collected | ai_order ...
    page_type     TEXT,            -- homepage | product | collection | cart | checkout | out_of_stock | other
    payload       JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages (conversation_id);
CREATE INDEX IF NOT EXISTS idx_events_conv ON events (conversation_id);
CREATE INDEX IF NOT EXISTS idx_conversations_topic ON conversations (topic);
