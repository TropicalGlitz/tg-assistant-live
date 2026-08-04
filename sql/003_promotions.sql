-- Migración 003: promociones con vigencia por fecha.
-- Arregla estructuralmente el bug de REP (promo "4th of July" expirada seguía saliendo):
-- el motor proactivo solo considera promos con active=true y dentro de [starts_at, ends_at].

CREATE TABLE IF NOT EXISTS promotions (
    id           BIGSERIAL PRIMARY KEY,
    code         TEXT,
    title        TEXT NOT NULL,
    description  TEXT,
    active       BOOLEAN NOT NULL DEFAULT true,
    starts_at    TIMESTAMPTZ,
    ends_at      TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_promotions_window ON promotions (active, starts_at, ends_at);

-- Ejemplo (desactivado por defecto):
-- INSERT INTO promotions (code, title, description, active, starts_at, ends_at)
-- VALUES ('SUMMER10', 'Summer Sale — 10% off', '10% off sitewide', true, now(), now() + interval '7 days');
