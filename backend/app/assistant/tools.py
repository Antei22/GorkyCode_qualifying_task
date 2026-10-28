import json
from datetime import date, datetime, time, timedelta
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from psycopg.types.json import Jsonb
from app.domain.models import DomainError, Kind, RecordDraft
from app.domain.time import parse_time, period_bounds, schedule


PENDING_TTL = timedelta(minutes=30)


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    @field_validator('kind', mode='before', check_fields=False)
    @classmethod
    def normalize_kind(cls, value):
        if not isinstance(value, str):
            return value
        aliases = {
            'task': 'task', 'задача': 'task', 'задачу': 'task', 'задачи': 'task',
            'meeting': 'meeting', 'встреча': 'meeting', 'встречу': 'meeting',
            'встречи': 'meeting', 'созвон': 'meeting',
            'reminder': 'reminder', 'напоминание': 'reminder',
            'напоминания': 'reminder',
        }
        return aliases.get(value.strip().lower().replace('ё', 'е'), value)


class CreateRecord(Arguments):
    """Создать задачу/встречу/напоминание или дополнить ожидающий черновик.

    date_expression/time_expression — выражения пользователя, а не придуманная
    дата. Для времени найденной встречи разрешён точный ISO день и HH:MM.
    Если данных мало, инструмент сохранит черновик и вернёт уточняющий вопрос.
    """
    mode: Literal['start', 'continue'] = Field(
        default='start',
        description='start для новой записи, continue только для ответа на вопрос по черновику',
    )
    kind: Kind | None = Field(default=None, description='Тип создаваемой записи')
    text: str | None = Field(default=None, min_length=1, max_length=1000,
                             description='Содержание без вводной команды')
    date_expression: str | None = Field(default=None, max_length=100,
                                        description='Выражение даты словами пользователя')
    time_expression: str | None = Field(default=None, max_length=50,
                                        description='Выражение времени словами пользователя')
    duration_minutes: int | None = Field(default=None, ge=5, le=480,
                                         description='Явно названная длительность события в минутах')


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


class CancelRecord(Arguments):
    """Отменить существующую запись по краткому описанию. Если найдено несколько записей, запросить уточнение."""
    query: str = Field(min_length=1, max_length=300,
                       description='Ключевые слова из описания отменяемой записи')


class AskClarification(Arguments):
    """Уточнить неоднозначный запрос. Ничего не создаёт."""
    question: str = Field(min_length=1, max_length=500)


MODELS = {'create_record': CreateRecord, 'list_records': ListRecords,
          'search_records': SearchRecords, 'cancel_record': CancelRecord,
          'ask_clarification': AskClarification}


def specifications():
    return [{'type': 'function', 'function': {
        'name': name, 'description': model.__doc__, 'parameters': model.model_json_schema()}}
        for name, model in MODELS.items()]


class Tools:
    def __init__(self, repo, con, owner, clock, pending):
        self.repo, self.con, self.owner, self.clock = repo, con, owner, clock
        self.pending = pending
        self.expired_pending = False
        self.created = []
        self.cancelled = []
        self.found = {}
        self.questions = []
        self.errors = []
        self.response_mode = None
        self.read_count = 0
        self.result_limit = 0
        self.signatures = {}
        if self._pending_is_expired():
            self.expired_pending = True
            self.save_pending(None)

    def _pending_is_expired(self):
        if not self.pending or not self.pending.get('updated_at'):
            return False
        try:
            updated_at = datetime.fromisoformat(self.pending['updated_at'])
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=self.clock.zone)
        except (TypeError, ValueError):
            return True
        return self.clock.now() - updated_at > PENDING_TTL

    def call(self, name, arguments):
        if name not in MODELS:
            return {'error': 'Unknown tool'}
        try:
            args = MODELS[name].model_validate(arguments)
            return getattr(self, name)(args)
        except DomainError as error:
            self.errors.append(str(error))
            return {'error': str(error)}
        except ValidationError as error:
            fields = {str(item['loc'][-1]) for item in error.errors() if item.get('loc')}
            if 'kind' in fields:
                message = 'Уточните тип записи: задача, встреча или напоминание.'
            else:
                message = 'Не удалось разобрать параметры запроса. Переформулируйте его.'
            self.errors.append(message)
            return {'error': message}

    def save_pending(self, pending):
        if pending:
            now = self.clock.now().isoformat()
            pending = dict(pending)
            pending['created_at'] = pending.get('created_at', now)
            pending['updated_at'] = now
        self.pending = pending
        self.con.execute('UPDATE sessions SET pending=%s WHERE id=%s',
                         (Jsonb(pending) if pending else None, self.owner))

    @staticmethod
    def _meeting_duration(text, explicit=None):
        if explicit is not None:
            return explicit
        value = text.lower().replace('ё', 'е')
        if any(word in value for word in ('конференц', 'воркшоп', 'тренинг', 'презентац')):
            return 120
        if any(word in value for word in ('деловая встреча', 'совещан', 'переговор', 'интервью')):
            return 90
        if any(word in value for word in ('созвон', 'звонок', 'позвонить', 'созвониться', 'телефон')):
            return 30
        return 60

    def _nearest_free_slot(self, requested, duration_minutes):
        first = requested.date()
        last = first + timedelta(days=30)
        meetings = self.repo.meetings_between(self.con, self.owner, first, last)
        by_day = {}
        for meeting in meetings:
            start_day = meeting['scheduled_date']
            start_time = meeting['scheduled_time']
            start = datetime.combine(start_day, start_time)
            length = meeting.get('duration_minutes') or 60
            end = start + timedelta(minutes=length)
            by_day.setdefault(start_day, []).append((start, end, meeting))

        requested_end = requested + timedelta(minutes=duration_minutes)
        initial_conflicts = [item for item in by_day.get(requested.date(), [])
                             if item[0] < requested_end and item[1] > requested]
        if not initial_conflicts:
            return None, None

        for offset in range(31):
            day = first + timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            opening = datetime.combine(day, time(9, 0))
            closing = datetime.combine(day, time(18, 0))
            candidate = max(requested, opening)
            if candidate >= closing or candidate + timedelta(minutes=duration_minutes) > closing:
                continue
            while candidate + timedelta(minutes=duration_minutes) <= closing:
                end = candidate + timedelta(minutes=duration_minutes)
                overlaps = [item for item in by_day.get(day, [])
                            if item[0] < end and item[1] > candidate]
                if not overlaps:
                    return initial_conflicts[0][2], candidate
                candidate = max(item[1] for item in overlaps)
        return initial_conflicts[0][2], None

    def create_record(self, args):
        if args.mode == 'continue':
            if not self.pending:
                if self.expired_pending:
                    raise DomainError('Черновик устарел. Начните создание заново.')
                raise DomainError('Нет незавершённого черновика. Начните создание заново.')
            data = dict(self.pending)
            data.pop('_suggested_slot', None)
            data.pop('created_at', None)
            data.pop('updated_at', None)
        else:
            data = {}
        original_data = dict(data)

        supplied = args.model_dump(exclude_none=True, exclude={'mode'})
        for key in ('kind', 'text', 'duration_minutes'):
            if key in supplied:
                data[key] = supplied[key]

        validation_errors = []
        if 'date_expression' in supplied:
            try:
                schedule(supplied['date_expression'], None, self.clock.now())
            except DomainError as error:
                validation_errors.append(str(error))
            else:
                data['date_expression'] = supplied['date_expression']
        if 'time_expression' in supplied:
            try:
                parse_time(supplied['time_expression'])
            except DomainError as error:
                validation_errors.append(str(error))
            else:
                data['time_expression'] = supplied['time_expression']

        if validation_errors:
            self.save_pending(data)
            question = validation_errors[0]
            self.questions.append(question)
            return {'needs_clarification': True, 'missing_field': 'invalid_value',
                    'suggested_question': question, 'draft': self.pending}

        day, moment = schedule(
            data.get('date_expression'),
            data.get('time_expression'),
            self.clock.now(),
        )
        now = self.clock.now()
        past_error = None
        past_field = None
        if day is not None and day < now.date():
            past_error = 'Указанная дата уже прошла. Выберите другую дату.'
            past_field = 'date_expression'
        elif day == now.date() and moment is not None:
            current_time = now.time().replace(tzinfo=None)
            if moment < current_time:
                past_error = 'Указанное время уже прошло. Выберите другое время.'
                past_field = ('time_expression' if 'time_expression' in supplied
                              else 'date_expression')
        if past_error:
            if past_field in original_data:
                data[past_field] = original_data[past_field]
            else:
                data.pop(past_field, None)
            self.save_pending(data)
            self.questions.append(past_error)
            return {'needs_clarification': True, 'missing_field': 'invalid_value',
                    'suggested_question': past_error, 'draft': self.pending}

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
            questions = {
                'kind': 'Что создать: задачу, встречу или напоминание?',
                'text': 'Что должно быть записано?',
                'date': 'На какую дату?',
                'time': 'На какое время?',
            }
            question = questions[missing[0]]
            self.questions.append(question)
            self.response_mode = 'clarification'
            return {'needs_clarification': True, 'missing_field': missing[0],
                    'suggested_question': question, 'draft': self.pending}
        if data['kind'] == 'meeting':
            duration = self._meeting_duration(data['text'], data.get('duration_minutes'))
            requested = datetime.combine(day, moment)
            conflict, nearest = self._nearest_free_slot(requested, duration)
            if conflict:
                data['duration_minutes'] = duration
                if nearest:
                    data['_suggested_slot'] = {
                        'date_expression': nearest.date().isoformat(),
                        'time_expression': nearest.strftime('%H:%M'),
                        'duration_minutes': duration,
                    }
                self.save_pending(data)
                if nearest:
                    question = (
                        f"На {day.strftime('%d.%m.%Y')} в {moment.strftime('%H:%M')} "
                        f"уже запланирована встреча «{conflict['text']}». "
                        f"Ближайшее свободное время — {nearest.strftime('%d.%m.%Y в %H:%M')}. "
                        f"Подтвердить?"
                    )
                else:
                    data.pop('_suggested_slot', None)
                    self.save_pending(data)
                    question = ('В ближайшие 30 дней не нашлось свободного времени '
                                'в рабочие часы. Укажите другую дату и время.')
                self.questions.append(question)
                return {'needs_confirmation': bool(nearest),
                        'missing_field': 'confirmation' if nearest else 'time',
                        'suggested_question': question,
                        'suggested_slot': data.get('_suggested_slot'),
                        'draft': self.pending}
            data['duration_minutes'] = duration
        draft = RecordDraft(kind=data['kind'], text=data['text'], scheduled_date=day,
                            scheduled_time=moment,
                            duration_minutes=data.get('duration_minutes'),
                            timezone=self.clock.zone.key)
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
        self.response_mode = 'read'
        self.result_limit = 100
        self.found.update({str(row['id']): row for row in rows})
        return {'records': rows, 'limit': 100, 'date_from': first, 'date_to': last,
                'note': 'Импортированные записи имеют неизвестный тип (kind=null).'}

    def search_records(self, args):
        rows = self.repo.search(self.con, self.owner, args.query, args.kind)
        self.read_count += 1
        self.response_mode = 'read'
        self.result_limit = 20
        self.found.update({str(row['id']): row for row in rows})
        return {'records': rows, 'limit': 20, 'includes_past': True,
                'note': 'Неизвестный тип импорта не доказывает, что запись — задача или встреча.'}

    def cancel_record(self, args):
        rows = self.repo.search(self.con, self.owner, args.query, limit=10)
        if not rows:
            self.errors.append('Не нашёл активную запись по этому описанию. Уточните название встречи или задачи.')
            return {'error': self.errors[-1]}
        if len(rows) > 1:
            self.questions.append('Нашёл несколько записей. Уточните дату или полное название той, которую нужно отменить.')
            self.response_mode = 'clarification'
            self.found.update({str(row['id']): row for row in rows})
            self.read_count += 1
            return {'needs_clarification': True, 'records': rows}
        record = self.repo.cancel_record(self.con, self.owner, rows[0]['id'])
        if not record:
            self.errors.append('Запись уже отменена или недоступна.')
            return {'error': self.errors[-1]}
        self.cancelled.append(record)
        return {'cancelled': record}

    def ask_clarification(self, args):
        self.questions.append(args.question)
        self.response_mode = 'clarification'
        return {'question': args.question}

    def encode(self, result):
        return json.dumps(result, ensure_ascii=False, default=str)

