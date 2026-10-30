"""PostgreSQL repository for sessions, records and chat history."""
from pathlib import Path
from datetime import datetime
from uuid import uuid4
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


class Repository:
    def __init__(self, dsn):
        self.dsn = dsn

    def connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5)

    def initialize(self):
        with self.connect() as connection:
            connection.execute('SELECT pg_advisory_xact_lock(7719042)')
            connection.execute('CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())')
            for path in sorted((Path(__file__).parent / 'migrations').glob('*.sql')):
                if not connection.execute('SELECT 1 FROM schema_migrations WHERE version=%s', (path.name,)).fetchone():
                    connection.execute(path.read_text(encoding='utf-8'))
                    connection.execute('INSERT INTO schema_migrations(version) VALUES (%s)', (path.name,))

    def exists(self, owner):
        with self.connect() as con:
            return con.execute('SELECT 1 FROM sessions WHERE id=%s', (owner,)).fetchone() is not None

    def new_session(self):
        owner = uuid4()
        with self.connect() as con:
            con.execute('INSERT INTO sessions(id) VALUES (%s)', (owner,))
        return owner

    @staticmethod
    def create(con, owner, draft, source='chat', source_id=None, raw=None):
        fields = draft.model_dump()
        return con.execute('''INSERT INTO records
            (id,owner_id,kind,kind_source,text,scheduled_date,scheduled_end_date,scheduled_time,duration_minutes,duration_source,duration_description,user_duration,duration_status,blocking_duration_source,timezone,source,source_id,raw_payload)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (uuid4(), owner, fields['kind'], fields['kind_source'], fields['text'], fields['scheduled_date'],
             fields['scheduled_end_date'], fields['scheduled_time'], fields['duration_minutes'], fields['duration_source'],
             fields['duration_description'], Jsonb(fields['user_duration']) if fields['user_duration'] else None,
             fields['duration_status'], fields['blocking_duration_source'], fields['timezone'],
             source, source_id, Jsonb(raw or {}))).fetchone()

    @staticmethod
    def create_with_id(con, owner, record_id, draft, source='chat'):
        fields = draft.model_dump()
        return con.execute('''INSERT INTO records
            (id,owner_id,kind,kind_source,text,scheduled_date,scheduled_end_date,scheduled_time,duration_minutes,
             duration_source,duration_description,user_duration,blocking_duration_source,
             duration_status,timezone,source,raw_payload)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb)
            RETURNING *''',
            (record_id, owner, fields['kind'], fields['kind_source'], fields['text'],
             fields['scheduled_date'], fields['scheduled_end_date'], fields['scheduled_time'],
             fields['duration_minutes'], fields['duration_source'],
             fields['duration_description'],
             Jsonb(fields['user_duration']) if fields['user_duration'] else None,
             fields['blocking_duration_source'], fields['duration_status'],
             fields['timezone'], source)).fetchone()

    @staticmethod
    def get_record(con, owner, record_id):
        return con.execute('''SELECT * FROM records
            WHERE owner_id=%s AND id=%s AND status <> 'cancelled' ''',
            (owner, record_id)).fetchone()

    @staticmethod
    def set_last_referenced(con, owner, record_id, kind):
        con.execute('''UPDATE sessions
            SET last_referenced_record_id=%s, last_referenced_kind=%s
            WHERE id=%s''', (record_id, kind, owner))

    @staticmethod
    def get_last_referenced(con, owner, kind=None):
        return con.execute('''SELECT records.* FROM sessions
            JOIN records ON records.id=sessions.last_referenced_record_id
            WHERE sessions.id=%s AND records.owner_id=%s
              AND records.status <> 'cancelled'
              AND (%s::text IS NULL OR records.kind=%s)''',
            (owner, owner, kind, kind)).fetchone()

    @staticmethod
    def resolve_records(con, owner, query=None, kind=None, temporal='unspecified',
                        day=None, moment=None, now=None, limit=20, statuses=None):
        now = (now or datetime.now()).replace(tzinfo=None)
        query = (query or '').strip() or None
        return con.execute('''SELECT * FROM records
            WHERE owner_id=%s AND status <> 'cancelled'
            AND (%s::text IS NULL OR kind=%s)
            AND (%s::text[] IS NULL OR status=ANY(%s::text[]))
            AND (%s::text IS NULL OR
                 to_tsvector('russian',text) @@ plainto_tsquery('russian',%s)
                 OR strpos(lower(text),lower(%s)) > 0)
            AND (%s::date IS NULL OR scheduled_date=%s)
            AND (%s::time IS NULL OR scheduled_time=%s)
            AND (%s IN ('unspecified','all')
                 OR (%s='future' AND (scheduled_date IS NULL OR
                     scheduled_date + COALESCE(scheduled_time,time '23:59:59') >= %s))
                 OR (%s='past' AND scheduled_date IS NOT NULL AND
                     scheduled_date + COALESCE(scheduled_time,time '23:59:59') < %s))
            ORDER BY
                CASE WHEN %s::text IS NULL THEN 0
                     WHEN lower(text)=lower(%s) THEN 0
                     WHEN strpos(lower(text),lower(%s)) > 0 THEN 1
                     ELSE 2 END,
                CASE WHEN scheduled_date + COALESCE(scheduled_time,time '23:59:59') >= %s THEN 0
                     WHEN scheduled_date IS NULL THEN 1 ELSE 2 END,
                CASE WHEN scheduled_date + COALESCE(scheduled_time,time '23:59:59') >= %s
                     THEN scheduled_date END ASC,
                CASE WHEN scheduled_date + COALESCE(scheduled_time,time '23:59:59') >= %s
                     THEN scheduled_time END ASC NULLS LAST,
                scheduled_date DESC NULLS LAST, scheduled_time DESC NULLS LAST, id
            LIMIT %s''',
            (owner, kind, kind, list(statuses) if statuses else None,
             list(statuses) if statuses else None, query, query, query, day, day, moment, moment,
             temporal, temporal, now, temporal, now,
             query, query, query, now, now, now, limit)).fetchall()

    @staticmethod
    def update_record(con, owner, record_id, expected_version, draft):
        fields = draft.model_dump()
        return con.execute('''UPDATE records SET kind=%s, kind_source=%s, text=%s,
            scheduled_date=%s, scheduled_end_date=%s, scheduled_time=%s, duration_minutes=%s,
            duration_source=%s, duration_description=%s, user_duration=%s,
            duration_status=%s, blocking_duration_source=%s, timezone=%s,
            version=version+1, updated_at=now()
            WHERE owner_id=%s AND id=%s AND version=%s AND status='open'
            RETURNING *''',
            (fields['kind'], fields['kind_source'], fields['text'], fields['scheduled_date'],
             fields['scheduled_end_date'], fields['scheduled_time'], fields['duration_minutes'],
             fields['duration_source'], fields['duration_description'],
             Jsonb(fields['user_duration']) if fields['user_duration'] else None,
             fields['duration_status'], fields['blocking_duration_source'],
             fields['timezone'], owner,
             record_id, expected_version)).fetchone()

    @staticmethod
    def set_status(con, owner, record_id, expected_version, status):
        return con.execute('''UPDATE records SET status=%s,
            version=version+1, updated_at=now()
            WHERE owner_id=%s AND id=%s AND version=%s AND status <> 'cancelled'
            RETURNING *, duration_minutes AS blocking_duration_minutes,
              CASE WHEN scheduled_date IS NOT NULL AND scheduled_time IS NOT NULL
                    AND duration_minutes IS NOT NULL
                   THEN scheduled_date + scheduled_time + duration_minutes * interval '1 minute'
              END AS reserved_until''',
            (status, owner, record_id, expected_version)).fetchone()

    @staticmethod
    def change_status_versioned(con, owner, record_id, expected_version, from_status, to_status):
        return con.execute('''UPDATE records SET status=%s,
            version=version+1, updated_at=now()
            WHERE owner_id=%s AND id=%s AND version=%s AND status=%s
            RETURNING *''',
            (to_status, owner, record_id, expected_version, from_status)).fetchone()

    @staticmethod
    def trace_search(con, context, intent, query, stage, rows):
        con.execute('''INSERT INTO operation_search_traces
            (owner_id,request_id,raw_text,parsed_intent,search_query,search_stage,candidate_ids)
            VALUES (%s,%s,%s,%s,%s,%s,%s)''',
            (context.owner_id, context.request_id, context.message,
             Jsonb(intent.model_dump(mode='json')), query, stage,
             Jsonb([str(row['id']) for row in rows])))

    @staticmethod
    def cancel_record_versioned(con, owner, record_id, expected_version, now):
        local_now = now.replace(tzinfo=None)
        return con.execute('''UPDATE records SET status='cancelled',
            version=version+1, updated_at=now()
            WHERE owner_id=%s AND id=%s AND version=%s AND status='open'
            AND (scheduled_date IS NULL OR
                 scheduled_date + COALESCE(scheduled_time,time '23:59:59') >= %s)
            RETURNING *''',
            (owner, record_id, expected_version, local_now)).fetchone()

    @staticmethod
    def list_records(con, owner, first=None, last=None, kind=None, limit=100,
                     now=None, kinds=None, temporal='all', order_direction='asc'):
        now = now or datetime.now()
        today, current_time = now.date(), now.time().replace(tzinfo=None)
        rows = con.execute('''SELECT records.*,
            duration_minutes AS blocking_duration_minutes,
            CASE WHEN scheduled_date IS NOT NULL AND scheduled_time IS NOT NULL
                      AND duration_minutes IS NOT NULL
                 THEN scheduled_date + scheduled_time
                      + duration_minutes * interval '1 minute' END AS reserved_until
            FROM records WHERE owner_id=%s AND status <> 'cancelled'
            AND (%s::date IS NULL OR scheduled_date >= %s)
            AND (%s::date IS NULL OR scheduled_date <= %s)
            AND (%s::text IS NULL OR kind=%s)
            AND (%s::text[] IS NULL OR kind=ANY(%s::text[]))
            ORDER BY
                CASE
                    WHEN scheduled_date > %s OR (scheduled_date=%s AND (scheduled_time IS NULL OR scheduled_time >= %s)) THEN 0
                    WHEN scheduled_date IS NULL THEN 1
                    ELSE 2
                END,
                CASE WHEN scheduled_date > %s OR (scheduled_date=%s AND (scheduled_time IS NULL OR scheduled_time >= %s)) THEN scheduled_date END ASC,
                CASE WHEN scheduled_date > %s OR (scheduled_date=%s AND (scheduled_time IS NULL OR scheduled_time >= %s)) THEN scheduled_time END ASC NULLS LAST,
                CASE WHEN scheduled_date < %s OR (scheduled_date=%s AND scheduled_time < %s) THEN scheduled_date END DESC,
                CASE WHEN scheduled_date < %s OR (scheduled_date=%s AND scheduled_time < %s) THEN scheduled_time END DESC NULLS LAST,
                id LIMIT %s''',
            (owner, first, first, last, last, kind, kind,
             list(kinds) if kinds else None, list(kinds) if kinds else None,
             today, today, current_time, today, today, current_time,
             today, today, current_time, today, today, current_time,
             today, today, current_time, limit)).fetchall()
        if temporal == 'future':
            rows = [row for row in rows if (
                row['scheduled_date'] is None
                or row['scheduled_date'] > today
                or (row['scheduled_date'] == today and
                    (row['scheduled_time'] is None or row['scheduled_time'] >= current_time))
            )]
        elif temporal == 'past':
            rows = [row for row in rows if row['scheduled_date'] is not None and (
                row['scheduled_date'] < today
                or (row['scheduled_date'] == today and row['scheduled_time'] is not None
                    and row['scheduled_time'] < current_time)
            )]
        rows.sort(key=lambda row: (
            row['scheduled_date'] is None,
            row['scheduled_date'] or today,
            row['scheduled_time'] or current_time,
            row['id'],
        ), reverse=order_direction == 'desc')
        return rows[:limit]

    @staticmethod
    def search(con, owner, query, kind=None, limit=20, now=None, future_only=False):
        # FTS handles Russian inflections; substring fallback handles name fragments.
        query = query.strip()
        now = now or datetime.now()
        today, current_time = now.date(), now.time().replace(tzinfo=None)
        return con.execute('''SELECT records.*,
            duration_minutes AS blocking_duration_minutes,
            CASE WHEN scheduled_date IS NOT NULL AND scheduled_time IS NOT NULL
                      AND duration_minutes IS NOT NULL
                 THEN scheduled_date + scheduled_time
                      + duration_minutes * interval '1 minute' END AS reserved_until
            FROM records WHERE owner_id=%s AND status <> 'cancelled'
            AND (%s::text IS NULL OR kind=%s)
            AND (NOT %s OR scheduled_date IS NULL OR scheduled_date > %s
                 OR (scheduled_date=%s AND (scheduled_time IS NULL OR scheduled_time >= %s)))
            AND (to_tsvector('russian',text) @@ plainto_tsquery('russian',%s)
                 OR strpos(lower(text),lower(%s)) > 0)
            ORDER BY
                CASE
                    WHEN scheduled_date > %s OR (scheduled_date=%s AND (scheduled_time IS NULL OR scheduled_time >= %s)) THEN 0
                    WHEN scheduled_date IS NULL THEN 1
                    ELSE 2
                END,
                CASE WHEN scheduled_date > %s OR (scheduled_date=%s AND (scheduled_time IS NULL OR scheduled_time >= %s)) THEN scheduled_date END ASC,
                CASE WHEN scheduled_date > %s OR (scheduled_date=%s AND (scheduled_time IS NULL OR scheduled_time >= %s)) THEN scheduled_time END ASC NULLS LAST,
                ts_rank(to_tsvector('russian',text),plainto_tsquery('russian',%s)) DESC,
                CASE WHEN scheduled_date < %s OR (scheduled_date=%s AND scheduled_time < %s) THEN scheduled_date END DESC,
                CASE WHEN scheduled_date < %s OR (scheduled_date=%s AND scheduled_time < %s) THEN scheduled_time END DESC NULLS LAST,
                id LIMIT %s''',
            (owner, kind, kind, future_only, today, today, current_time,
             query, query, today, today, current_time, today, today, current_time,
             today, today, current_time, query, today, today, current_time,
             today, today, current_time, limit)).fetchall()

    @staticmethod
    def meetings_between(con, owner, first, last):
        return con.execute('''SELECT * FROM records
            WHERE owner_id=%s AND kind='meeting' AND status <> 'cancelled'
            AND scheduled_date BETWEEN %s AND %s AND scheduled_time IS NOT NULL
            ORDER BY scheduled_date, scheduled_time, id''',
            (owner, first, last)).fetchall()

    @staticmethod
    def duration_candidates(con, owner, query, now, temporal='future', day=None):
        # Untyped imports remain untyped; only meeting-shaped titles are candidates.
        local_now = now.replace(tzinfo=None)
        return con.execute('''SELECT * FROM records
            WHERE owner_id=%s AND status <> 'cancelled'
            AND (kind='meeting' OR (kind IS NULL AND text ~* '^(встреча|созвон)([[:space:]]|$)'))
            AND (to_tsvector('russian',text) @@ plainto_tsquery('russian',%s)
                 OR strpos(lower(text),lower(%s)) > 0)
            AND (%s::date IS NULL OR scheduled_date=%s)
            AND (%s='all' OR (%s='future' AND scheduled_date + COALESCE(scheduled_time, time '23:59:59') >= %s)
                 OR (%s='past' AND scheduled_date + COALESCE(scheduled_time, time '23:59:59') < %s))
            ORDER BY
                CASE WHEN %s='past' THEN scheduled_date END DESC,
                CASE WHEN %s='past' THEN scheduled_time END DESC,
                scheduled_date, scheduled_time, id LIMIT 20''',
            (owner, query, query, day, day, temporal, temporal, local_now,
             temporal, local_now, temporal, temporal)).fetchall()

    @staticmethod
    def update_duration(con, owner, record_id, minutes, source, description, now):
        return con.execute('''UPDATE records SET duration_minutes=%s,
            duration_source=%s, duration_description=%s, updated_at=now()
            WHERE owner_id=%s AND id=%s AND kind='meeting' AND status <> 'cancelled'
            AND scheduled_date + COALESCE(scheduled_time, time '23:59:59') >= %s
            RETURNING *''', (minutes, source, description, owner, record_id,
                             now.replace(tzinfo=None))).fetchone()

    @staticmethod
    def cancel_record(con, owner, record_id, now=None):
        now = now or datetime.now()
        today, current_time = now.date(), now.time().replace(tzinfo=None)
        return con.execute('''UPDATE records SET status='cancelled'
            WHERE owner_id=%s AND id=%s AND status <> 'cancelled'
            AND (scheduled_date IS NULL OR scheduled_date > %s
                 OR (scheduled_date=%s AND (scheduled_time IS NULL OR scheduled_time >= %s)))
            RETURNING *''',
            (owner, record_id, today, today, current_time)).fetchone()

    @staticmethod
    def history(con, owner, limit=20):
        return list(reversed(con.execute('''SELECT role,content FROM chat_messages
            WHERE owner_id=%s ORDER BY id DESC LIMIT %s''', (owner, limit)).fetchall()))

    @staticmethod
    def remember(con, owner, role, content):
        con.execute('INSERT INTO chat_messages(owner_id,role,content) VALUES (%s,%s,%s)',
                    (owner, role, content))

