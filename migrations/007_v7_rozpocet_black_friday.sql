-- v7 (14. 10. 2026): Hlídání podle rozpočtu + Black Friday rozesílka.
-- Aplikace tabulky vytvoří sama při startu (init_db → create_all, idempotentní).
-- Tento skript je pro ruční / auditované nasazení na Neon (psql "$DATABASE_URL" -f …). Bezpečné spustit opakovaně.
BEGIN;
CREATE TABLE IF NOT EXISTS budget_watches (
    id SERIAL PRIMARY KEY,
    token VARCHAR(64) NOT NULL UNIQUE,
    category_slug VARCHAR(40) NOT NULL,
    keyword VARCHAR(60),
    max_price NUMERIC(12, 2) NOT NULL,
    only_in_stock BOOLEAN NOT NULL DEFAULT FALSE,
    channel VARCHAR(10) NOT NULL DEFAULT 'push',
    endpoint TEXT,
    p256dh VARCHAR(200),
    auth VARCHAR(100),
    email VARCHAR(320),
    confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_sent_at TIMESTAMPTZ,
    sent_day VARCHAR(10),
    sent_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_budget_watches_category_slug ON budget_watches (category_slug);
CREATE INDEX IF NOT EXISTS ix_budget_endpoint ON budget_watches (endpoint);
CREATE TABLE IF NOT EXISTS budget_hits (
    id SERIAL PRIMARY KEY,
    budget_id INTEGER NOT NULL REFERENCES budget_watches (id) ON DELETE CASCADE,
    product_id INTEGER NOT NULL REFERENCES products (id) ON DELETE CASCADE,
    price NUMERIC(12, 2) NOT NULL,
    notified BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_budget_hits_budget_id ON budget_hits (budget_id);
CREATE INDEX IF NOT EXISTS ix_budget_hits_product_id ON budget_hits (product_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_budget_hit ON budget_hits (budget_id, product_id);
CREATE TABLE IF NOT EXISTS topic_sends (
    id SERIAL PRIMARY KEY,
    key VARCHAR(60) NOT NULL UNIQUE,
    sent_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent INTEGER NOT NULL DEFAULT 0,
    items TEXT
);
COMMIT;
