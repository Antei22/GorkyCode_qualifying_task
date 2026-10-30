ALTER TABLE records
    ADD COLUMN user_duration jsonb,
    ADD COLUMN blocking_duration_source text NOT NULL DEFAULT 'legacy_unknown'
        CHECK (blocking_duration_source IN (
            'user_exact','user_approximate','user_range_max',
            'qualitative_default','event_type_default','legacy_unknown'
        ));

UPDATE records
SET user_duration = jsonb_strip_nulls(jsonb_build_object(
        'type', 'exact', 'minutes', duration_minutes,
        'evidence', duration_description
    )),
    blocking_duration_source = 'user_exact'
WHERE duration_source = 'user' AND duration_minutes IS NOT NULL;

UPDATE records
SET user_duration = jsonb_build_object(
        'type', 'range', 'min_minutes', 60, 'max_minutes', 120,
        'evidence', duration_description
    ),
    blocking_duration_source = 'user_range_max'
WHERE duration_source = 'estimated'
  AND duration_description ~* '^\s*час\s*[-–—]\s*два\s*$';

UPDATE records
SET user_duration = jsonb_build_object(
        'type', 'qualitative',
        'value', CASE WHEN duration_description ~* 'недолго|быстро|коротк' THEN 'short'
                      ELSE 'long' END,
        'evidence', duration_description
    ),
    blocking_duration_source = 'qualitative_default'
WHERE duration_source = 'estimated' AND duration_description IS NOT NULL
  AND user_duration IS NULL;

UPDATE records SET blocking_duration_source = 'event_type_default'
WHERE duration_source = 'estimated' AND duration_description IS NULL;

CREATE TABLE operation_search_traces (
    id bigserial PRIMARY KEY,
    owner_id uuid NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    request_id uuid NOT NULL,
    raw_text text NOT NULL,
    parsed_intent jsonb NOT NULL,
    search_query text,
    search_stage text NOT NULL,
    candidate_ids jsonb NOT NULL DEFAULT '[]',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX operation_search_traces_owner_request
    ON operation_search_traces(owner_id, request_id);
