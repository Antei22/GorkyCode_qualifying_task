"""PostgreSQL repository for sessions, records and chat history."""
from pathlib import Path
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
            (id,owner_id,kind,text,scheduled_date,scheduled_time,timezone,source,source_id,raw_payload)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (uuid4(), owner, fields['kind'], fields['text'], fields['scheduled_date'],
             fields['scheduled_time'], fields['timezone'], source, source_id, Jsonb(raw or {}))).fetchone()

    @staticmethod
    def list_records(con, owner, first=None, last=None, kind=None, limit=100):
        return con.execute('''SELECT * FROM records WHERE owner_id=%s AND status <> 'cancelled'
            AND (%s::date IS NULL OR scheduled_date >= %s)
            AND (%s::date IS NULL OR scheduled_date <= %s)
            AND (%s::text IS NULL OR kind=%s)
            ORDER BY scheduled_date NULLS LAST, scheduled_time NULLS LAST, id LIMIT %s''',
            (owner, first, first, last, last, kind, kind, limit)).fetchall()

    @staticmethod
    def search(con, owner, query, kind=None, limit=20):
        # FTS handles Russian inflections; substring fallback handles name fragments.
        query = query.strip()
        return con.execute('''SELECT * FROM records WHERE owner_id=%s AND status <> 'cancelled'
            AND (%s::text IS NULL OR kind=%s)
            AND (to_tsvector('russian',text) @@ plainto_tsquery('russian',%s)
                 OR strpos(lower(text),lower(%s)) > 0)
            ORDER BY ts_rank(to_tsvector('russian',text),plainto_tsquery('russian',%s)) DESC,
                scheduled_date DESC NULLS LAST, id LIMIT %s''',
            (owner, kind, kind, query, query, query, limit)).fetchall()

    @staticmethod
    def history(con, owner, limit=20):
        return list(reversed(con.execute('''SELECT role,content FROM chat_messages
            WHERE owner_id=%s ORDER BY id DESC LIMIT %s''', (owner, limit)).fetchall()))

    @staticmethod
    def remember(con, owner, role, content):
        con.execute('INSERT INTO chat_messages(owner_id,role,content) VALUES (%s,%s,%s)',
                    (owner, role, content))

