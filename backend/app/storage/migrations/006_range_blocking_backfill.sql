UPDATE records
SET duration_minutes = (user_duration->>'max_minutes')::integer,
    duration_source = 'estimated',
    blocking_duration_source = 'user_range_max'
WHERE user_duration->>'type' = 'range'
  AND user_duration ? 'max_minutes'
  AND duration_minutes IS DISTINCT FROM (user_duration->>'max_minutes')::integer;
