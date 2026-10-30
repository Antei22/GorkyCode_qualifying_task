import json
from datetime import date, datetime, time, timedelta
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from psycopg.types.json import Jsonb
from app.domain.models import DomainError, Kind, RecordDraft
from app.domain.time import parse_day, parse_time, period_bounds, schedule
from app.domain.duration import cancellation_requested, duration_request, explicit_duration


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
    duration_unspecified: bool = Field(default=False,
        description='Пользователь прямо сказал, что не знает длительность; не подставлять выдуманные минуты')


class GetMeetingDuration(Arguments):
    """Узнать длительность встречи. Будущее и история доступны отдельно; оценки не являются точными данными."""
    query: str = Field(min_length=1, max_length=300)
    temporal: Literal['future', 'past', 'all'] = 'future'
    date_expression: str | None = None


class UpdateMeetingDuration(Arguments):
    """Изменить длительность существующей будущей встречи. Никогда не отменяет и не создаёт запись."""
    mode: Literal['start', 'continue'] = 'start'
    query: str | None = Field(default=None, min_length=1, max_length=300)
    date_expression: str | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    duration_description: str | None = Field(default=None, max_length=300,
        description='Слова пользователя без точных минут, например «долго» или «не знаю»')


class ListRecords(Arguments):
    """Показать записи за период. Без kind возвращаются все три типа."""
    period: Literal['today', 'tomorrow', 'day_after_tomorrow', 'this_week', 'next_week', 'all', 'custom']
    kind: Kind | None = None
    date_from: str = ''
    date_to: str = ''


class SearchRecords(Arguments):
    """Поиск в том числе в прошлом. Короткая смысловая фраза, не полная команда.
    """
    query: str = Field(min_length=1, max_length=300)
    kind: Kind | None = None


class CancelRecord(Arguments):
    """Отменить существующую запись по краткому описанию. Если найдено несколько записей, запросить уточнение."""
    query: str = Field(min_length=1, max_length=300,
                       description='Ключевые слова из описания отменяемой записи')
    kind: Kind | None = Field(default=None, description='Явно названный пользователем тип записи')


class AskClarification(Arguments):
    """Уточнить неоднозначный запрос. Ничего не создаёт."""
    question: str = Field(min_length=1, max_length=500)


MODELS = {'create_record': CreateRecord, 'list_records': ListRecords,
          'search_records': SearchRecords, 'cancel_record': CancelRecord,
          'get_meeting_duration': GetMeetingDuration,
          'update_meeting_duration': UpdateMeetingDuration,
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
        self.updated = []
        self.answers = []
        self.user_message = ''
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
            if name == 'cancel_record' and not cancellation_requested(self.user_message):
                raise DomainError('Отмена не запрошена. Для изменения длительности используйте изменение встречи.')
            intent = duration_request(self.user_message)
            if intent and intent['action'] == 'read' and name in {'create_record', 'update_meeting_duration'}:
                raise DomainError('Это вопрос о длительности. Изменение записи не запрошено.')
            if intent and intent['action'] == 'update' and name == 'create_record':
                raise DomainError('Измените существующую встречу через update_meeting_duration.')
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
    def default_meeting_duration(text):
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
            if (self.pending or {}).get('_duration_update'):
                raise DomainError('Ожидается изменение длительности существующей встречи, а не создание новой.')
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

        supplied = args.model_dump(exclude_none=True, exclude={'mode', 'duration_unspecified'})
        for key in ('kind', 'text', 'duration_minutes'):
            if key in supplied:
                data[key] = supplied[key]
        if args.duration_minutes is not None:
            data['_duration_source'] = (
                'user' if explicit_duration(self.user_message) == args.duration_minutes
                else original_data.get('_duration_source', 'unknown')
                if original_data.get('duration_minutes') == args.duration_minutes else 'unknown')
            data.pop('_duration_description', None)
        elif args.duration_unspecified:
            description = self.user_message.strip()
            data['duration_minutes'] = (120 if 'долго' in description.lower()
                                        else self.default_meeting_duration(data.get('text', '')))
            data['_duration_source'] = 'estimated'
            data['_duration_description'] = description

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
        if data.get('kind') == 'meeting' and data.get('duration_minutes') is None:
            missing.append('duration')
        if moment is not None and day is None:
            missing.append('date')
        if missing:
            self.save_pending(data)
            questions = {
                'kind': 'Что создать: задачу, встречу или напоминание?',
                'text': 'Что должно быть записано?',
                'date': 'На какую дату?',
                'time': 'На какое время?',
                'duration': 'Сколько будет длиться встреча?',
            }
            question = questions[missing[0]]
            self.questions.append(question)
            self.response_mode = 'clarification'
            return {'needs_clarification': True, 'missing_field': missing[0],
                    'suggested_question': question, 'draft': self.pending}
        if data['kind'] == 'meeting':
            duration = data['duration_minutes']
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
                            duration_source=data.get('_duration_source', 'unknown'),
                            duration_description=data.get('_duration_description'),
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
        rows = self.repo.list_records(self.con, self.owner, first, last, args.kind,
                                      now=self.clock.now())
        self.read_count += 1
        self.response_mode = 'read'
        self.result_limit = 100
        self.found.update({str(row['id']): row for row in rows})
        return {'records': rows, 'limit': 100, 'date_from': first, 'date_to': last,
                'note': 'Импортированные записи классифицированы по kind.'}

    def search_records(self, args):
        rows = self.repo.search(self.con, self.owner, args.query, args.kind,
                                now=self.clock.now())
        self.read_count += 1
        self.response_mode = 'read'
        self.result_limit = 20
        self.found.update({str(row['id']): row for row in rows})
        return {'records': rows, 'limit': 20, 'includes_past': True,
                'note': 'Неизвестный тип импорта не доказывает, что запись — задача или встреча.'}

    def cancel_record(self, args):
        now = self.clock.now()
        rows = self.repo.search(self.con, self.owner, args.query, args.kind, limit=10,
                                now=now, future_only=True)
        if not rows:
            past_rows = self.repo.search(self.con, self.owner, args.query, args.kind,
                                         limit=1, now=now)
            if past_rows:
                self.errors.append('Эта запись уже прошла, поэтому отменить её нельзя.')
                return {'error': self.errors[-1]}
            self.errors.append('Не нашёл активную запись по этому описанию. Уточните название встречи или задачи.')
            return {'error': self.errors[-1]}
        if len(rows) > 1:
            self.questions.append('Нашёл несколько записей. Уточните дату или полное название той, которую нужно отменить.')
            self.response_mode = 'clarification'
            self.found.update({str(row['id']): row for row in rows})
            self.read_count += 1
            return {'needs_clarification': True, 'records': rows}
        record = self.repo.cancel_record(self.con, self.owner, rows[0]['id'], now=now)
        if not record:
            self.errors.append('Запись уже отменена или недоступна.')
            return {'error': self.errors[-1]}
        self.cancelled.append(record)
        return {'cancelled': record}

    def _duration_candidates(self, query, temporal, date_expression=None):
        day = parse_day(date_expression, self.clock.now()) if date_expression else None
        if day and temporal == 'past' and day > self.clock.now().date() and not any(c.isdigit() for c in (date_expression or '')[-4:]):
            day = day.replace(year=day.year - 1)
        return self.repo.duration_candidates(self.con, self.owner, query, self.clock.now(),
                                             temporal='all' if day else temporal, day=day)

    def get_meeting_duration(self, args):
        intent = duration_request(self.user_message)
        temporal = intent['temporal'] if intent and intent['action'] == 'read' else args.temporal
        date_expression = (intent or {}).get('date_expression') or args.date_expression
        rows = self._duration_candidates(args.query, temporal, date_expression)
        self.found.update({str(row['id']): row for row in rows})
        if not rows:
            answer = 'Не нашёл встречу по этому описанию и времени. Уточните дату или название.'
        else:
            lines = []
            for row in rows:
                label = f"«{row['text']}» — {row['scheduled_date']} {row['scheduled_time'] or ''}"
                if row.get('duration_source') == 'user' and row.get('duration_minutes') is not None:
                    detail = f"указанная плановая длительность — {row['duration_minutes']} мин."
                else:
                    detail = 'Точная длительность не указана.'
                    if row.get('duration_description') and 'долго' in row['duration_description'].lower():
                        detail += ' Указано только, что встреча будет долгой.'
                    if row.get('duration_minutes') is not None:
                        detail += ' Для расписания используется приблизительная оценка.'
                if row['scheduled_date'] and datetime.combine(row['scheduled_date'], row['scheduled_time'] or time.max) < self.clock.now().replace(tzinfo=None):
                    detail += ' Фактическая продолжительность не зафиксирована.'
                lines.append(label + ': ' + detail)
            answer = '\n'.join(lines)
            if len(rows) > 1:
                answer += '\nНайдено несколько встреч. Уточните дату нужной встречи.'
            if len(rows) == 20:
                answer += '\nПоказаны первые 20 совпадений.'
        self.answers.append(answer)
        return {'answer': answer, 'records': rows}

    def update_meeting_duration(self, args):
        continuing = args.mode == 'continue' and (self.pending or {}).get('_duration_update')
        intent = duration_request(self.user_message)
        if not continuing and not (intent and intent['action'] == 'update'):
            raise DomainError('Изменение длительности не запрошено.')
        query = args.query or (continuing or {}).get('query')
        if not query:
            raise DomainError('Уточните, у какой встречи изменить длительность.')
        date_expression = args.date_expression or (continuing or {}).get('date_expression')
        rows = self._duration_candidates(query, 'future', date_expression)
        if continuing and continuing.get('id'):
            rows = [row for row in rows if str(row['id']) == continuing['id']]
        if not rows:
            raise DomainError('Не нашёл будущую встречу для изменения длительности.')
        if len(rows) > 1:
            minutes = args.duration_minutes if args.duration_minutes is not None else (continuing or {}).get('minutes')
            if minutes is not None and explicit_duration(self.user_message) != minutes and (continuing or {}).get('minutes') != minutes:
                raise DomainError('Уточните длительность в минутах или часах: точное значение не указано.')
            self.save_pending({'_duration_update': {
                'query': query, 'minutes': minutes,
                'description': args.duration_description or (continuing or {}).get('description'),
            }})
            self.found.update({str(row['id']): row for row in rows})
            self.questions.append('Найдено несколько встреч. Уточните дату нужной встречи:\n' + '\n'.join(
                f"• {r['text']} — {r['scheduled_date']} {r['scheduled_time'] or ''}" for r in rows))
            return {'needs_clarification': True, 'records': rows}
        row = rows[0]
        if row['kind'] != 'meeting':
            raise DomainError('Тип импортированной записи неизвестен. Изменение её длительности недоступно.')
        description = args.duration_description or (continuing or {}).get('description')
        minutes = args.duration_minutes if args.duration_minutes is not None else (continuing or {}).get('minutes')
        if minutes is None and not description:
            self.save_pending({'_duration_update': {
                'id': str(row['id']), 'query': query, 'date_expression': date_expression}})
            self.questions.append('Какую длительность указать? Можно назвать минуты или сказать, что точное время неизвестно.')
            return {'needs_clarification': True}
        source = 'user' if minutes is not None else 'estimated'
        if (minutes is not None and explicit_duration(self.user_message) != minutes
                and (continuing or {}).get('minutes') != minutes):
            raise DomainError('Уточните длительность в минутах или часах: точное значение не указано.')
        if minutes is None:
            minutes = 120 if 'долго' in description.lower() else self.default_meeting_duration(row['text'])
        record = self.repo.update_duration(self.con, self.owner, row['id'], minutes,
                                           source, description, self.clock.now())
        if not record:
            raise DomainError('Встреча уже прошла, отменена или недоступна.')
        self.updated.append(record)
        if (self.pending or {}).get('_duration_update'):
            self.save_pending(None)
        # Expanding a duration can introduce a conflict; report it without moving events.
        start = datetime.combine(record['scheduled_date'], record['scheduled_time'])
        meetings = self.repo.meetings_between(self.con, self.owner, start.date() - timedelta(days=1), start.date() + timedelta(days=1))
        for other in meetings:
            if other['id'] == record['id']:
                continue
            other_start = datetime.combine(other['scheduled_date'], other['scheduled_time'])
            if other_start < start + timedelta(minutes=minutes) and other_start + timedelta(minutes=other.get('duration_minutes') or 60) > start:
                self.answers.append(f"Обновлённая встреча пересекается с «{other['text']}» — {other['scheduled_date']} {other['scheduled_time']}.")
        return {'updated': record}

    def ask_clarification(self, args):
        self.questions.append(args.question)
        self.response_mode = 'clarification'
        return {'question': args.question}

    def encode(self, result):
        return json.dumps(result, ensure_ascii=False, default=str)

