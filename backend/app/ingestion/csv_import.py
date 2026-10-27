import argparse
import csv
from datetime import date, time
from pathlib import Path
from uuid import UUID, uuid5, NAMESPACE_URL
from psycopg.types.json import Jsonb
from app.config import Settings
from app.storage.repository import Repository
from zoneinfo import ZoneInfo

SOURCE = 'voice_assistant_events_1000'


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ['id', 'date', 'time', 'text']:
            raise ValueError('Expected CSV columns: id,date,time,text')
        rows, seen = [], set()
        for number, row in enumerate(reader, 2):
            if None in row or any(not str(row.get(k) or '').strip() for k in reader.fieldnames):
                raise ValueError(f'Line {number}: missing or extra fields')
            if row['id'] in seen:
                raise ValueError(f'Line {number}: duplicate source id')
            seen.add(row['id'])
            rows.append((row, date.fromisoformat(row['date']), time.fromisoformat(row['time'])))
        if not rows:
            raise ValueError('Empty CSV')
        return rows


def import_csv(repo, owner, path, timezone):
    ZoneInfo(timezone)
    rows = read_csv(path)  # Validate the complete source before opening a transaction.
    imported = 0
    with repo.connect() as con:
        con.execute('SELECT id FROM sessions WHERE id=%s FOR UPDATE', (owner,))
        for raw, day, moment in rows:
            uid = uuid5(NAMESPACE_URL, f'gorkycode:{owner}:{SOURCE}:{raw["id"]}')
            got = con.execute('''INSERT INTO records
                (id,owner_id,kind,text,scheduled_date,scheduled_time,timezone,source,source_id,raw_payload)
                VALUES (%s,%s,NULL,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(owner_id,source,source_id) DO NOTHING RETURNING id''',
                (uid, owner, raw['text'], day, moment, timezone, SOURCE, raw['id'], Jsonb(raw))).fetchone()
            imported += int(got is not None)
    return {'total': len(rows), 'imported': imported, 'skipped': len(rows)-imported}


def main():
    parser = argparse.ArgumentParser(description='Import CSV into an existing browser session')
    parser.add_argument('path')
    parser.add_argument('--owner', required=True, type=UUID, help='Session UUID shown in the UI')
    args = parser.parse_args()
    settings = Settings()
    repo = Repository(settings.database_url)
    repo.initialize()
    if not repo.exists(args.owner):
        parser.error('Session does not exist. Open the app first.')
    print(import_csv(repo, args.owner, args.path, settings.timezone))


if __name__ == '__main__':
    main()
