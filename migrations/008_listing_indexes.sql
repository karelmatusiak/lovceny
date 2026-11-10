-- Additive indexes for /black-friday and /slevy (safe to re-run).
-- price_points(offer_id, captured_at) is what the history load uses.
-- offers(last_checked) makes the cache stamp MAX(last_checked) an index lookup.
-- create_all does not add indexes to tables that already exist; app startup runs the same SQL.

CREATE INDEX IF NOT EXISTS ix_pp_offer_time ON price_points (offer_id, captured_at);
CREATE INDEX IF NOT EXISTS ix_offers_last_checked ON offers (last_checked);
