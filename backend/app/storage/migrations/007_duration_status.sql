ALTER TABLE records
    ADD COLUMN duration_status text NOT NULL DEFAULT 'defaulted'
        CHECK (duration_status IN (
            'missing','provided','declined_to_specify','defaulted'
        ));

UPDATE records
SET duration_status = 'provided'
WHERE user_duration IS NOT NULL;
