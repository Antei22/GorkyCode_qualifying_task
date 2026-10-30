ALTER TABLE records
    ADD COLUMN kind_source text;

WITH classified AS (
    SELECT id,
        CASE
            WHEN lower(trim(text)) ~ '^(напомнить|напоминание)\y'
                THEN 'reminder'
            WHEN lower(trim(text)) ~ '^(встреча|созвон|демо|совещание)\y'
              OR lower(trim(text)) ~ '^провести[[:space:]]+план(е|ё)рк'
              OR lower(trim(text)) ~ '^обсудить\y.*[[:space:]]с[[:space:]]+[^[:space:]]+'
                THEN 'meeting'
            WHEN lower(trim(text)) ~ '^(подготовить|проверить|отправить|обновить|собрать|передать|согласовать|подготовиться|позвонить)\y'
                THEN 'task'
            ELSE 'task'
        END AS detected_kind,
        CASE
            WHEN lower(trim(text)) ~ '^(напомнить|напоминание)\y'
              OR lower(trim(text)) ~ '^(встреча|созвон|демо|совещание)\y'
              OR lower(trim(text)) ~ '^провести[[:space:]]+план(е|ё)рк'
              OR lower(trim(text)) ~ '^обсудить\y.*[[:space:]]с[[:space:]]+[^[:space:]]+'
              OR lower(trim(text)) ~ '^(подготовить|проверить|отправить|обновить|собрать|передать|согласовать|подготовиться|позвонить)\y'
                THEN 'import_rule'
            ELSE 'import_fallback'
        END AS detected_source
    FROM records
    WHERE kind IS NULL
)
UPDATE records AS record
SET kind = classified.detected_kind,
    kind_source = classified.detected_source
FROM classified
WHERE record.id = classified.id;

UPDATE records
SET kind_source = CASE
    WHEN source = 'chat' THEN 'user_input'
    ELSE 'import_rule'
END
WHERE kind_source IS NULL;

ALTER TABLE records
    ALTER COLUMN kind SET NOT NULL,
    ALTER COLUMN kind_source SET DEFAULT 'user_input',
    ALTER COLUMN kind_source SET NOT NULL;

ALTER TABLE records
    ADD CONSTRAINT records_kind_source_check CHECK (kind_source IN (
        'import_rule', 'import_semantic_classifier', 'import_fallback',
        'user_input', 'explicit_user_command', 'explicit_entity_word',
        'semantic_inference', 'user_clarification', 'system_default'
    ));
