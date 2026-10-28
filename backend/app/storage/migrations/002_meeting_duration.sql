ALTER TABLE records
    ADD COLUMN duration_minutes integer CHECK (duration_minutes BETWEEN 5 AND 480);
