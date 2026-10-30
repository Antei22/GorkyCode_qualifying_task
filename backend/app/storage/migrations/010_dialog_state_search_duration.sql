ALTER TABLE sessions
    ADD COLUMN last_referenced_record_id uuid REFERENCES records(id),
    ADD COLUMN last_referenced_kind text CHECK (
        last_referenced_kind IN ('task', 'meeting', 'reminder')
    );
