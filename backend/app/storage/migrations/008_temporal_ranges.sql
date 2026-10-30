ALTER TABLE records
    ADD COLUMN scheduled_end_date date;

CREATE INDEX records_active_range
    ON records(owner_id, status, scheduled_date, scheduled_end_date);
