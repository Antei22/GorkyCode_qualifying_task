import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from psycopg.types.json import Jsonb
from app.domain.models import DomainError, Kind, RecordDraft
from app.domain.time import period_bounds, schedule


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class CreateRecord(Arguments):
    """Создать задачу/встречу/напоминание или дополнить ожидающий черновик.

    date_expression/time_expression — выражения пользователя, а не придуманная
    дата. Для времени найденной встречи разрешён точный ISO день и HH:MM.
    Если данных мало, инструмент сохранит черновик и вернёт уточняющий вопрос.
    """
    kind: Kind | None = None
    text: str | None = Field(default=None, min_length=1, max_length=1000)
    date_expression: str | None = Field(default=None, max_length=100)
    time_expression: str | None = Field(default=None, max_length=50)


class ListRecords(Arguments):
    """Показать записи за период. kind=null включает импорт с неизвестным типом."""
    period: Literal['today', 'tomorrow', 'day_after_tomorrow', 'this_week', 'next_week', 'all', 'custom']
    kind: Kind | None = None
    date_from: str = ''
    date_to: str = ''


class SearchRecords(Arguments):
    """Поиск в том числе в прошлом. Короткая смысловая фраза, не полная команда.

    Импорт не размечен: для него kind=null; объясни пользователю неизвестный тип.
    """
    query: str = Field(min_length=1, max_length=300)
    kind: Kind | None = None


class AskClarification(Arguments):
    """Уточнить неоднозначный запрос. Ничего не создаёт."""
    question: str = Field(min_length=1, max_length=500)


MODELS = {'create_record': CreateRecord, 'list_records': ListRecords,
          'search_records': SearchRecords, 'ask_clarification': AskClarification}


def specifications():
    return [{'type': 'function', 'function': {
        'name': name, 'description': model.__doc__, 'parameters': model.model_json_schema()}}
        for name, model in MODELS.items()]


class Tools:
    def __init__(self, repo, con, owner, clock, pending):
        self.repo, self.con, self.owner, self.clock = repo, con, owner, clock
        self.pending = pending
        self.created = []
        self.found = {}
        self.questions = []
        self.errors = []
        self.read_count = 0
        self.result_limit = 0
        self.signatures = {}

    def call(self, name, arguments):
        if name not in MODELS:
            return {'error': 'Unknown tool'}
        try:
            args = MODELS[name].model_validate(arguments)
            return getattr(self, name)(args)
        except (DomainError, ValidationError) as error:
            self.errors.append(str(error))
            return {'error': str(error)}

    def save_pending(self, pending):
        self.pending = pending
        self.con.execute('UPDATE sessions SET pending=%s WHERE id=%s',
                         (Jsonb(pending) if pending else None, self.owner))

    def create_record(self, args):
        data = dict(self.pending or {})
        data.update(args.model_dump(exclude_none=True))
        day, moment = schedule(data.get('date_expression'), data.get('time_expression'), self.clock.now())
        missing = [key for key in ('kind', 'text') if not data.get(key)]
        if data.get('kind') in ('meeting', 'reminder'):
            if day is None:
                missing.append('date')
            if moment is None:
                missing.append('time')
        if moment is not None and day is None:
            missing.append('date')
        if missing:
            self.save_pending(data)
            labels = {'kind': 'тип записи', 'text': 'содержание', 'date': 'дату', 'time': 'время'}
            question = 'Уточните ' + ', '.join(labels[key] for key in missing) + '.'
            self.questions.append(question)
            return {'needs_clarification': True, 'question': question, 'draft': data}
        draft = RecordDraft(kind=data['kind'], text=data['text'], scheduled_date=day,
                            scheduled_time=moment, timezone=self.clock.zone.key)
        signature = draft.model_dump_json()
        if signature in self.signatures:
            return {'created': self.signatures[signature], 'reused': True}
        record = self.repo.create(self.con, self.owner, draft)
        self.created.append(record)
        self.signatures[signature] = record
        self.save_pending(None)
        return {'created': record}

    def list_records(self, args):
        first, last = period_bounds(args.period, self.clock.now(), args.date_from, args.date_to)
        rows = self.repo.list_records(self.con, self.owner, first, last, args.kind)
        self.read_count += 1
        self.result_limit = 100
        self.found.update({str(row['id']): row for row in rows})
        return {'records': rows, 'limit': 100, 'date_from': first, 'date_to': last,
                'note': 'Импортированные записи имеют неизвестный тип (kind=null).'}

    def search_records(self, args):
        rows = self.repo.search(self.con, self.owner, args.query, args.kind)
        self.read_count += 1
        self.result_limit = 20
        self.found.update({str(row['id']): row for row in rows})
        return {'records': rows, 'limit': 20, 'includes_past': True,
                'note': 'Неизвестный тип импорта не доказывает, что запись — задача или встреча.'}

    def ask_clarification(self, args):
        self.questions.append(args.question)
        return {'question': args.question}

    def encode(self, result):
        return json.dumps(result, ensure_ascii=False, default=str)

