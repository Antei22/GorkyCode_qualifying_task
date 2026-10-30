import argparse
import csv
import json
import logging
from datetime import date, time
from pathlib import Path
from uuid import UUID, uuid5, NAMESPACE_URL
from psycopg.types.json import Jsonb
from app.config import Settings
from app.ingestion.kind_classifier import (
    SemanticImportKindClassifier, classify_import_record,
)
from app.storage.repository import Repository
from zoneinfo import ZoneInfo

SOURCE = 'voice_assistant_events_1000'
logger = logging.getLogger(__name__)


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


def import_csv(repo, owner, path, timezone, semantic_classifier=None):
    ZoneInfo(timezone)
    rows = read_csv(path)  # Validate the complete source before opening a transaction.
    imported = 0
    counts = {'meeting': 0, 'task': 0, 'reminder': 0, 'fallback': 0}
    with repo.connect() as con:
        con.execute('SELECT id FROM sessions WHERE id=%s FOR UPDATE', (owner,))
        for raw, day, moment in rows:
            uid = uuid5(NAMESPACE_URL, f'gorkycode:{owner}:{SOURCE}:{raw["id"]}')
            classification = classify_import_record(
                raw['text'], semantic_classifier,
            )
            got = con.execute('''INSERT INTO records
                (id,owner_id,kind,kind_source,text,scheduled_date,scheduled_time,
                 timezone,source,source_id,raw_payload)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(owner_id,source,source_id) DO NOTHING RETURNING id''',
                (uid, owner, classification.kind, classification.source,
                 raw['text'], day, moment, timezone, SOURCE, raw['id'],
                 Jsonb(raw))).fetchone()
            if got is not None:
                imported += 1
                counts[classification.kind] += 1
                counts['fallback'] += int(classification.fallback_used)
            logger.info('import_kind %s', json.dumps({
                'record_id': str(uid), 'text': raw['text'],
                'kind': classification.kind,
                'kind_source': classification.source,
                'classifier_result': classification.classifier_result,
                'fallback_used': classification.fallback_used,
            }, ensure_ascii=False))
    return {
        'total': len(rows), 'imported': imported, 'skipped': len(rows)-imported,
        'meeting_count': counts['meeting'], 'task_count': counts['task'],
        'reminder_count': counts['reminder'],
        'fallback_count': counts['fallback'], 'null_count': 0,
    }


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
    classifier = SemanticImportKindClassifier(settings)
    print(import_csv(
        repo, args.owner, args.path, settings.timezone,
        semantic_classifier=classifier,
    ))


if __name__ == '__main__':
    main()
