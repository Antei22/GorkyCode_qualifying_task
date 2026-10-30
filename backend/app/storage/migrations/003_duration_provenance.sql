ALTER TABLE records
    ADD COLUMN duration_source text NOT NULL DEFAULT 'unknown'
        CHECK (duration_source IN ('user', 'estimated', 'unknown')),
    ADD COLUMN duration_description text;

-- Existing values cannot reliably be attributed to the user or to a heuristic.
-- Keep them unknown rather than declaring old estimates exact.
