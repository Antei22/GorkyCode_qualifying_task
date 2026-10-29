ALTER TABLE sessions
    ADD COLUMN operation_state jsonb;

ALTER TABLE records
    ADD COLUMN version bigint NOT NULL DEFAULT 1;

CREATE INDEX records_active_target
    ON records(owner_id, status, scheduled_date, scheduled_time);
