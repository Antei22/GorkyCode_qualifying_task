"""Structured LLM adapter. It proposes intent and never executes a mutation."""
import json
import logging
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from openai import OpenAI

from app.domain.duration import duration_request, explicit_duration, parse_user_duration
from app.domain.models import DomainError
from app.domain.time import (
    ParsedDate, ParsedTime, normalize_date_payload, normalize_time_payload,
    resolve_date, resolve_time,
)
from app.domain.operations import (
    Action, DurationInput, Interpretation, OperationField, OperationIntent,
    PendingOperation, Phase, RecordPatch, TargetQuery, TurnKind,
)


logger = logging.getLogger(__name__)


class ProposalPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: str | None = None
    text: str | None = None
    date: ParsedDate | None = None
    time: ParsedTime | None = None
    timezone: str | None = None
    user_duration: DurationInput | None = None


class Proposal(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    turn: TurnKind = TurnKind.UNKNOWN
    action: Action = Action.UNKNOWN
    patch: ProposalPatch | None = Field(
        default=None,
        description='Только явно извлечённые из текущей реплики значения.',
    )
    target_query: str | None = None
    target_kind: str | None = None
    target_temporal: str = 'unspecified'
    target_date: ParsedDate | None = None
    target_time: ParsedTime | None = None
    kind: str | None = None
    kind_ambiguous: bool = Field(
        default=False,
        description=(
            'Пользователь выразил намерение выполнить действие, но не выбрал, '
            'создать само событие/встречу или напоминание о нём.'
        ),
    )
    text: str | None = None
    date: ParsedDate | None = Field(default=None, description=(
        'Семантическая дата: absolute, relative, month_day, weekday, '
        'start_of_period или end_of_period. Добавь evidence из реплики.'
    ))
    time: ParsedTime | None = Field(
        default=None,
        description=(
            'Время начала из новой реплики. Учитывай ожидаемое поле и русские '
            'разговорные формы: при вопросе о времени «на 4 дня»/«в 4 дня» '
            'означает 16:00, а не длительность четыре дня.'
        ),
    )
    timezone: str | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    duration_description: str | None = None
    user_duration: DurationInput | None = Field(
        default=None,
        description=(
            'Длительность ровно в той точности, которую сообщил пользователь: '
            'exact, approximate, range или qualitative. evidence обязательно.'
        ),
    )
    clear_fields: tuple[Literal['kind', 'text', 'date', 'time', 'timezone',
                                'duration'], ...] = ()
    read_fields: tuple[str, ...] = Field(default=(), alias='requested_fields')
    unknown_fields: tuple[Literal[
        'kind', 'text', 'date_expression', 'time_expression', 'timezone', 'duration',
    ], ...] = ()
    request_alternative: bool = Field(
        default=False,
        description=(
            'Пользователь отклоняет предложенный свободный слот и просит '
            'показать другое ближайшее время.'
        ),
    )
    ambiguity_resolution: OperationField | None = Field(
        default=None,
        description=(
            'Поле, роль которого пользователь выбрал в ответ на текущее '
            'уточнение ambiguous_fields. Значение повторять не требуется.'
        ),
    )
    selected_candidate_number: int | None = Field(default=None, ge=1, le=20)


class AuditFinding(BaseModel):
    """One source-grounded addition or ambiguity found after extraction."""
    model_config = ConfigDict(extra='forbid')
    field: OperationField
    disposition: Literal['explicit', 'ambiguous']
    value: str | None = None
    date: ParsedDate | None = None
    time: ParsedTime | None = None
    evidence: str = Field(min_length=1)
    ambiguous_with: OperationField | None = None


class AuditResult(BaseModel):
    model_config = ConfigDict(extra='forbid')
    findings: tuple[AuditFinding, ...] = ()


INSTRUCTIONS = '''Ты преобразуешь русскую реплику в одну структурированную операцию.
Ты ничего не выполняешь и не утверждаешь, что данные сохранены.
action: create, update, cancel, read или unknown.
turn=new для самостоятельной новой команды; continue только для ответа на текущий
вопрос; confirm только для явного подтверждения предложенного плана; abandon для
отказа от незавершённого черновика. Изменение даты, времени, текста, типа или
длительности существующей записи всегда update, никогда cancel/create.
Для явной коррекции вроде "нет, не в 10, давай в 11" верни turn=correction.
Поля с префиксом target_ описывают СТАРУЮ запись; поля без префикса — только
желаемое новое состояние. text — содержание создаваемой записи или новый текст.
Даты возвращай структурами date/target_date, время — time/target_time.
Абсолютная дата: {type:absolute, year, month, day}; без года: {type:month_day,
month, day, year:null, resolution:nearest_future}; сдвиг: {type:relative,
days_offset}. День недели передавай как weekday (0=пн…6=вс) и week_offset;
границы периода — start_of_period/end_of_period с period. Всегда добавляй evidence,
точную цитату из реплики. Для "с завтра на пятницу" завтра — target_date, пятница —
date. Не придумывай отсутствующие дату, время, тип и минуты.
Время возвращай как {type:absolute, hour, minute, evidence}. Если пользователь
назвал только час, minute=0. Не ищи минуты среди соседних чисел. Числа внутри
даты нельзя повторно использовать как час, минуты, длительность или другое поле.
Один фрагмент исходной реплики нельзя использовать для двух полей, если значение
не было явно названо дважды. Evidence даты и времени должны быть раздельными.
Примеры:
"Создай встречу с мамой в 10 30.10.26" → date={type:absolute,year:2026,
month:10,day:30,evidence:"30.10.26"}, time={type:absolute,hour:10,minute:0,
evidence:"10"}. Значение time=10:30 здесь ошибочно: 30 является частью даты.
"Создай встречу в 15 25.12.26" → time=15:00, date=25.12.2026.
"Создай встречу в 15:30 25.12.26" → time=15:30, date=25.12.2026.
Встреча и напоминание — разные типы. Если пользователь лишь выражает намерение
созвониться, увидеться, встретиться или выполнить синонимичное действие, но явно
не называет тип записи, оставь kind пустым и верни kind_ambiguous=true. Сервер
должен уточнить: создать само событие/встречу или напоминание о действии.
Для вопроса "сколько будет длиться" используй
read, requested_fields=["duration"], target_temporal=future; "сколько длилась" — past.
target_query содержит только идентифицирующую часть записи, без вопросительных и
служебных слов. Например, "Сколько будет длиться встреча с мамой" →
target_query="встреча с мамой", requested_fields=["duration"]. "Когда встреча с мамой" →
тот же target_query, requested_fields=["date","time"]. Не копируй всю реплику в query.
Длительность возвращай в user_duration, сохраняя точность пользователя:
"на 2 часа" → exact/120; "примерно 2 часа" → approximate/120;
"час-два" → range/60/120; "долго" → qualitative/long. Всегда приложи evidence.
Не своди диапазон или приблизительное значение к точным минутам.
Если пользователь явно просит убрать поле, добавь его имя в clear_fields; обычное
отсутствие поля не означает очистку.
Если пользователь прямо сообщает, что НЕ ЗНАЕТ или не хочет уточнять значение поля
(например, "хз", "не знаю", "без понятия", "неважно", "оставь как есть"),
добавь ожидаемое поле в unknown_fields. Это применимо
к любому полю, не только к длительности. Не оставляй такой ответ просто пустым.
Текущая операция содержит expected_field — это смысл вопроса, на который отвечает
новая реплика. Если expected_field=time_expression, разговорные ответы «на 4 дня»,
«в 4 дня», «на 4 часа дня» означают время начала 16:00. Не относись к ним как к
duration. Если expected_field=duration, та же числовая конструкция может означать
длительность. Контекст ожидаемого поля важнее изолированного прочтения фразы.
Expected field означает обязательное сейчас поле, но не запрещает извлекать другие
явно указанные значения. В follow-up верни все такие значения в patch текущей
реплики. Не решай, можно ли заменять старое значение: это делает backend merge.
Если система предложила свободное время и ждёт подтверждения, а пользователь просит
другое время (например, «мне неудобно, какое ещё есть?», «покажи следующий вариант»),
верни turn=continue, action=unknown и request_alternative=true. Это продолжение
текущей операции, не read/update и не новая команда.
Если текущая операция содержит ambiguous_fields и пользователь выбирает одну из
предложенных ролей (например, отвечает «длительность»), верни выбранное имя поля в
ambiguity_resolution. Не проси его повторять исходное значение: оно хранится сервером.
Если пользователь выбирает вариант из показанного списка, верни его номер, не UUID.
Одна реплика описывает одну операцию. Обязательно вызови propose_operation.'''


AUDIT_INSTRUCTIONS = '''Проверь структурированный разбор пользовательской реплики.
Ищи только явно присутствующие, но пропущенные значения полей, а также выражения,
роль которых неоднозначна. Не заполняй поле только потому, что оно обязательно.
Для каждого finding приведи точную непрерывную цитату evidence из новой реплики.
Для date_expression заполняй поле date структурой ParsedDate, для time_expression —
поле time структурой ParsedTime; не возвращай даты и время строками.
disposition=explicit допустим только при единственной смысловой роли; value содержит
извлечённое значение. disposition=ambiguous используй, если одна цитата может означать
разные поля; укажи второе поле в ambiguous_with и не придумывай value.
Учитывай expected_field текущего диалога: ответ на явный вопрос о времени относится
к time, ответ на вопрос о длительности — к duration. Уже заполненные поля
не повторяй, кроме случая, когда их роль на самом деле неоднозначна.
Если первичный разбор выбрал meeting/reminder, хотя пользователь лишь выразил
намерение и не назвал тип записи, отметь kind как ambiguous.
Если новых или неоднозначных значений нет, верни пустой findings.'''


class LLMOperationInterpreter:
    def __init__(self, settings, client_factory=OpenAI):
        self.settings = settings
        self.client_factory = client_factory

    def interpret(self, context, pending: PendingOperation | None) -> Interpretation:
        deterministic = self._turn(context.message, pending)
        if deterministic is not None:
            return deterministic
        if not self.settings.llm_api_key:
            raise DomainError(
                'Для чата настройте LLM_API_KEY, LLM_BASE_URL и LLM_MODEL в .env.'
            )
        pending_context = self._pending_context(pending)
        history = '\n'.join(f'{role}: {content}' for role, content in context.history[-8:])
        messages = [
            {'role': 'system', 'content': INSTRUCTIONS},
            {'role': 'user', 'content': (
                f'Сейчас: {context.now.isoformat()}\n'
                f'Timezone: {getattr(context.now.tzinfo, "key", str(context.now.tzinfo))}\n'
                f'Текущая операция: {json.dumps(pending_context, ensure_ascii=False)}\n'
                f'Недавний диалог:\n{history}\n'
                f'Новая реплика: {context.message}'
            )},
        ]
        specification = {'type': 'function', 'function': {
            'name': 'propose_operation',
            'description': 'Предложить разбор одной пользовательской операции без выполнения.',
            'parameters': Proposal.model_json_schema(),
        }}
        with self.client_factory(
            base_url=self.settings.llm_base_url,
            api_key=self.settings.llm_api_key,
            timeout=self.settings.llm_timeout,
            max_retries=0,
        ) as client:
            response = client.chat.completions.create(
                model=self.settings.llm_model,
                messages=messages,
                tools=[specification],
                tool_choice={'type': 'function', 'function': {'name': 'propose_operation'}},
                temperature=0,
            )
        calls = response.choices[0].message.tool_calls or []
        if len(calls) != 1 or calls[0].function.name != 'propose_operation':
            raise DomainError('Модель не смогла однозначно разобрать операцию.')
        try:
            proposal = self._parse_proposal(calls[0].function.arguments)
        except (ValueError, TypeError, ValidationError) as error:
            raise DomainError('Модель вернула некорректный разбор операции.') from error
        proposal = self._normalize_action_fields(proposal, pending)
        proposal = self._normalize_pending_answer(context.message, pending, proposal)
        ambiguous_fields = ()
        ambiguity_values = {}
        ambiguity_evidence = None
        # A request for another proposed slot is a conversation-control signal,
        # not record content.  Running field recovery after normalization would
        # turn phrases such as "what other time is there?" into a new title.
        if not proposal.request_alternative:
            if self._needs_audit(context.message, proposal, pending):
                (proposal, ambiguous_fields, ambiguity_values,
                 ambiguity_evidence) = self._audit(
                    context, pending, proposal,
                )
            if (proposal.action == Action.CREATE and proposal.kind_ambiguous
                    and not self._has_explicit_kind_label(context.message)):
                proposal = proposal.model_copy(update={'kind': None})
                ambiguous_fields = tuple(sorted(set(ambiguous_fields) | {'kind'}))
                ambiguity_evidence = ambiguity_evidence or context.message
            duration_source = (
                pending.intent.ambiguity_evidence
                if pending is not None and proposal.ambiguity_resolution == 'duration'
                else context.message
            )
            proposal = self._verify_duration(duration_source or context.message, proposal)
            proposal = self._normalize_read_target(context.message, proposal)
            proposal = self._verify_temporal_evidence(context, proposal)
            if proposal.ambiguity_resolution is None:
                proposal = self._recover_create_text(context.message, proposal, pending)
        selected = None
        if proposal.selected_candidate_number is not None:
            if pending is None or proposal.selected_candidate_number > len(pending.candidate_ids):
                raise DomainError('Выбранного варианта нет в текущем списке.')
            selected = pending.candidate_ids[proposal.selected_candidate_number - 1]
        try:
            target_values = {}
            if proposal.target_query is not None:
                target_values['query'] = proposal.target_query
            if proposal.target_kind is not None:
                target_values['kind'] = self._kind(proposal.target_kind)
            temporal = self._temporal(proposal.target_temporal)
            if temporal != 'unspecified':
                target_values['temporal'] = temporal
            if proposal.target_date is not None:
                target_values['date_expression'] = resolve_date(
                    proposal.target_date, context.now,
                ).isoformat()
            if proposal.target_time is not None:
                target_values['time_expression'] = resolve_time(
                    proposal.target_time,
                ).isoformat(timespec='minutes')
            target = TargetQuery(**target_values)
            change_values = {key: value for key, value in {
                    'kind': self._kind(proposal.kind),
                    'text': proposal.text,
                    'date_expression': (resolve_date(
                        proposal.date, context.now,
                    ).isoformat() if proposal.date is not None else None),
                    'time_expression': (resolve_time(
                        proposal.time,
                    ).isoformat(timespec='minutes') if proposal.time is not None else None),
                    'timezone': proposal.timezone,
                    'duration': self._duration_input(proposal),
                }.items() if value is not None}
            clear_names = {'kind': 'kind', 'text': 'text', 'date': 'date_expression',
                           'time': 'time_expression', 'timezone': 'timezone',
                           'duration': 'duration'}
            for name in proposal.clear_fields:
                change_values[clear_names[name]] = None
            changes = RecordPatch(**change_values)
        except ValidationError as error:
            fields = ', '.join('.'.join(str(part) for part in item['loc'])
                               for item in error.errors())
            raise DomainError(
                f'Модель вернула неподдерживаемые параметры операции: {fields}.'
            ) from error
        intent_values = dict(
            action=proposal.action,
            source_text=context.message,
            field_evidence={key: value.evidence for key, value in {
                'date_expression': proposal.date,
                'time_expression': proposal.time,
                'target.date_expression': proposal.target_date,
                'target.time_expression': proposal.target_time,
            }.items() if value is not None and value.evidence},
            target=target,
            changes=changes,
            read_fields=proposal.read_fields,
            unknown_fields=proposal.unknown_fields,
            ambiguous_fields=ambiguous_fields,
            ambiguity_values=ambiguity_values,
            ambiguity_evidence=ambiguity_evidence,
            resolved_ambiguity=(proposal.ambiguity_resolution
                                if pending is not None
                                and proposal.ambiguity_resolution
                                in pending.intent.ambiguous_fields else None),
        )
        if proposal.request_alternative:
            intent_values['alternative_requested'] = True
        intent = OperationIntent(**intent_values)
        turn = proposal.turn
        # Some compact models label an imperative as "continue" merely because it
        # refers to an existing record. Without server pending, a complete concrete
        # action is necessarily a new operation.
        if (pending is None and turn in (TurnKind.CONTINUE, TurnKind.UNKNOWN)
                and proposal.action != Action.UNKNOWN):
            turn = TurnKind.NEW
        elif pending is not None and turn == TurnKind.UNKNOWN:
            turn = TurnKind.CONTINUE
        return Interpretation(turn=turn, intent=intent,
                              selected_candidate_id=selected)

    @classmethod
    def _needs_audit(cls, message, proposal, pending):
        """Use a second model pass only when the first pass may have lost data."""
        if proposal.action not in (Action.CREATE, Action.UPDATE):
            return False
        value = message.lower().replace('ё', 'е')
        expected = cls._expected_field(pending)
        if proposal.action == Action.CREATE and proposal.text is None:
            return True
        if (proposal.action == Action.CREATE and proposal.kind is None and re.search(
                r'\b(?:задач\w*|встреч\w*|напоминан\w*)\b', value)):
            return True
        if (proposal.action == Action.CREATE
                and proposal.kind in {'meeting', 'reminder'}
                and not cls._has_explicit_kind_label(message)):
            return True
        if (proposal.date is None
                and proposal.target_date is None
                and cls._date_phrases(message)):
            return True
        clock_like = re.search(
            r'\b(?:[01]?\d|2[0-3]):[0-5]\d\b|'
            r'\b(?:в|на)\s+\d{1,2}(?:\s+час\w*)?(?:\s+(?:дня|вечера|утра|ночи))?\b',
            value,
        )
        if clock_like and (proposal.time is None
                           or expected == 'time_expression'):
            return True
        duration_like = re.search(
            r'\b(?:\d+|один|одна|два|две|три|четыре|полтора)\s*'
            r'(?:минут\w*|час\w*)\b|\b(?:долго|недолго)\b', value,
        )
        if duration_like and (proposal.duration_minutes is None
                              and proposal.duration_description is None):
            return True
        if (proposal.timezone is None
                and re.search(r'\b(?:utc|gmt|мск|moscow|москва)\b', value)):
            return True
        # "на 4 часа" deserves an explicit semantic review even if the first
        # pass already chose either start time or duration.
        return bool(re.search(r'\bна\s+\d{1,2}\s+час\w*\b', value))

    @staticmethod
    def _has_explicit_kind_label(message):
        value = message.lower().replace('ё', 'е')
        return bool(re.search(
            r'\b(?:задач\w*|встреч\w*|напоминан\w*|событи\w*)\b', value,
        ))

    def _audit(self, context, pending, proposal):
        pending_context = self._pending_context(pending)
        messages = [
            {'role': 'system', 'content': AUDIT_INSTRUCTIONS},
            {'role': 'user', 'content': (
                f'Текущая операция: {json.dumps(pending_context, ensure_ascii=False)}\n'
                f'Первичный разбор: {proposal.model_dump_json(exclude_none=True)}\n'
                f'Новая реплика: {context.message}'
            )},
        ]
        specification = {'type': 'function', 'function': {
            'name': 'audit_operation',
            'description': 'Проверить пропущенные и неоднозначные поля без выполнения операции.',
            'parameters': AuditResult.model_json_schema(),
        }}
        with self.client_factory(
            base_url=self.settings.llm_base_url,
            api_key=self.settings.llm_api_key,
            timeout=self.settings.llm_timeout,
            max_retries=0,
        ) as client:
            response = client.chat.completions.create(
                model=self.settings.llm_model,
                messages=messages,
                tools=[specification],
                tool_choice={'type': 'function', 'function': {'name': 'audit_operation'}},
                temperature=0,
            )
        calls = response.choices[0].message.tool_calls or []
        if len(calls) != 1 or calls[0].function.name != 'audit_operation':
            return proposal, (), {}, None
        try:
            audit = AuditResult.model_validate_json(calls[0].function.arguments)
        except (ValueError, TypeError, ValidationError):
            return proposal, (), {}, None
        return self._apply_audit(context.message, pending, proposal, audit)

    @classmethod
    def _apply_audit(cls, message, pending, proposal, audit):
        updates = {}
        ambiguous = set()
        ambiguity_values = {}
        ambiguity_evidence = None
        expected = cls._expected_field(pending)
        for finding in audit.findings:
            if not cls._evidence_present(message, finding.evidence):
                continue
            if finding.disposition == 'ambiguous':
                fields = {finding.field}
                if finding.ambiguous_with is not None:
                    fields.add(finding.ambiguous_with)
                if not cls._plausibly_ambiguous(finding.evidence, fields, expected):
                    continue
                ambiguous.update(fields)
                ambiguity_evidence = ambiguity_evidence or finding.evidence
                for field in fields:
                    if field == 'duration':
                        updates['duration_minutes'] = None
                        updates['duration_description'] = None
                    else:
                        updates[field] = None
                continue
            if (finding.value is None and not (
                    finding.field == 'date_expression' and finding.date is not None
                    or finding.field == 'time_expression' and finding.time is not None)):
                continue
            field = finding.field
            if field == 'duration':
                if (proposal.duration_minutes is not None
                        or proposal.duration_description is not None):
                    continue
                minutes = explicit_duration(finding.evidence)
                if minutes is not None:
                    updates['duration_minutes'] = minutes
                elif cls._evidence_present(message, finding.value):
                    updates['duration_description'] = finding.value
                continue
            proposal_field = cls._proposal_field(field)
            if getattr(proposal, proposal_field) is not None:
                continue
            if field == 'kind':
                normalized = cls._kind(finding.value)
                if normalized is not None:
                    updates['kind'] = normalized
            elif field == 'text':
                if cls._evidence_present(message, finding.value):
                    updates['text'] = finding.value
            elif field == 'date_expression' and finding.date is not None:
                updates['date'] = finding.date
            elif field == 'time_expression' and finding.time is not None:
                updates['time'] = finding.time
            else:
                updates[field] = finding.value
        # Some temporal phrases have two valid record roles.  The audit model is
        # not allowed to silently choose one when the surrounding sentence and
        # dialogue do not disambiguate it.  This guard is role-based rather than
        # tied to one command wording.
        if cls._has_time_duration_collision(message, expected):
            ambiguous.update({'time_expression', 'duration'})
            match = cls._time_duration_collision_match(message)
            if match is not None:
                ambiguity_evidence = match.group(0)
                ambiguity_values['time_expression'] = match.group('number')
            if proposal.duration_minutes is not None:
                ambiguity_values['duration'] = str(proposal.duration_minutes)
            elif proposal.duration_description is not None:
                ambiguity_values['duration'] = proposal.duration_description
            else:
                minutes = explicit_duration(match.group(0)) if match else None
                if minutes is not None:
                    ambiguity_values['duration'] = str(minutes)
        if ambiguous:
            # An ambiguity cannot coexist with a supposedly concrete extraction.
            for field in ambiguous:
                if field == 'duration':
                    updates['duration_minutes'] = None
                    updates['duration_description'] = None
                else:
                    updates[cls._proposal_field(field)] = None
        return (proposal.model_copy(update=updates) if updates else proposal,
                tuple(sorted(ambiguous)), ambiguity_values, ambiguity_evidence)

    @staticmethod
    def _proposal_field(field):
        return {
            'date_expression': 'date', 'time_expression': 'time',
            'target_date_expression': 'target_date',
            'target_time_expression': 'target_time',
        }.get(field, field)

    @staticmethod
    def _has_time_duration_collision(message, expected):
        if expected in {'time_expression', 'duration'}:
            return False
        value = message.lower().replace('ё', 'е')
        if LLMOperationInterpreter._time_duration_collision_match(value) is None:
            return False
        # A day part makes it a clock time; explicit duration language makes it
        # a duration.  Without either cue both readings remain possible.
        if re.search(r'\b(?:дня|вечера|утра|ночи)\b', value):
            return False
        if re.search(
            r'\b(?:длительност\w*|длиться|длится|продолжаться|продлится|'
            r'продолжительност\w*|на протяжении)\b', value,
        ):
            return False
        return True

    @staticmethod
    def _time_duration_collision_match(message):
        number = (
            r'(?P<number>\d{1,2}|один|одна|два|две|три|четыре|пять|шесть|семь|'
            r'восемь|девять|десять|одиннадцать|двенадцать)'
        )
        return re.search(rf'\bна\s+{number}\s+час\w*\b',
                         message.lower().replace('ё', 'е'))

    @staticmethod
    def _evidence_present(message, evidence):
        normalize = lambda value: ' '.join(value.lower().replace('ё', 'е').split())
        return bool(evidence.strip()) and normalize(evidence) in normalize(message)

    @staticmethod
    def _plausibly_ambiguous(evidence, fields, expected):
        if fields != {'time_expression', 'duration'}:
            return True
        if expected in fields:
            return False
        value = evidence.lower().replace('ё', 'е')
        if re.search(r'\b\d{1,2}:[0-5]\d\b', value):
            return False
        if re.search(r'\b(?:дня|вечера|утра|ночи)\b', value):
            return False
        return bool(re.search(r'\b\d{1,2}\s+час\w*\b', value))

    @staticmethod
    def _turn(message, pending):
        value = message.strip().lower().replace('ё', 'е').strip(' .,!')
        if pending and value in {'да', 'подтверждаю', 'подтвердить', 'согласен',
                                 'согласна', 'подходит', 'ок', 'окей'}:
            return Interpretation(turn=TurnKind.CONFIRM)
        if pending and value in {'нет', 'не надо', 'отказаться'}:
            return Interpretation(turn=TurnKind.REJECT)
        if pending and value in {'отмена', 'отмени черновик', 'отмени создание'}:
            return Interpretation(turn=TurnKind.ABANDON)
        return None

    @staticmethod
    def _verify_duration(message, proposal):
        if (proposal.user_duration is None
                and proposal.duration_minutes is None
                and proposal.duration_description is None):
            return proposal
        parsed = parse_user_duration(message)
        if parsed is not None:
            return proposal.model_copy(update={
                'user_duration': DurationInput.model_validate(parsed),
                'duration_minutes': None, 'duration_description': None,
            })
        updates = {}
        if proposal.user_duration is not None:
            evidence = proposal.user_duration.evidence
            if not evidence or not LLMOperationInterpreter._evidence_present(
                    message, evidence):
                updates['user_duration'] = None
        if proposal.duration_minutes is not None:
            actual = explicit_duration(message)
            if actual != proposal.duration_minutes:
                updates['duration_minutes'] = None
        return proposal.model_copy(update=updates) if updates else proposal

    @staticmethod
    def _duration_input(proposal):
        if proposal.user_duration is not None:
            return proposal.user_duration.model_dump(exclude_none=True)
        if (proposal.duration_minutes is not None
                or proposal.duration_description is not None):
            return {
                'minutes': proposal.duration_minutes,
                'description': proposal.duration_description,
            }
        return None

    @staticmethod
    def _normalize_read_target(message, proposal):
        """Enforce the server-verifiable duration-query split.

        The LLM remains responsible for the general semantic target.  For the
        sensitive duration read already recognized by the deterministic domain
        rule, the question words can never leak into database search.
        """
        parsed = duration_request(message)
        if (not parsed or parsed['action'] != 'read'
                or proposal.action not in {Action.READ, Action.UNKNOWN}):
            return proposal
        return proposal.model_copy(update={
            'action': Action.READ,
            'target_query': parsed['query'],
            'target_kind': proposal.target_kind or 'meeting',
            'target_temporal': parsed['temporal'],
            'read_fields': tuple(dict.fromkeys((*proposal.read_fields, 'duration'))),
        })

    @classmethod
    def _verify_temporal_evidence(cls, context, proposal):
        """Retain structured temporal values only when their evidence is in-source."""
        updates = {}
        for field in ('date', 'target_date', 'time', 'target_time'):
            value = getattr(proposal, field)
            if value is not None and value.evidence and not cls._evidence_present(
                    context.message, value.evidence):
                updates[field] = None
            elif value is not None and field in {'date', 'target_date'}:
                try:
                    resolve_date(value, context.now)
                except (DomainError, ValueError, OverflowError):
                    updates[field] = None
        return proposal.model_copy(update=updates) if updates else proposal

    @staticmethod
    def _date_phrases(message):
        value = message.lower().replace('ё', 'е')
        months = ('января|февраля|марта|апреля|мая|июня|июля|августа|'
                  'сентября|октября|ноября|декабря')
        weekdays = ('понедельник|вторник|среду|среда|четверг|пятницу|'
                    'пятница|субботу|суббота|воскресенье')
        patterns = (
            r'\b(?:послезавтра|сегодня|завтра|вчера)\b',
            r'\b\d{4}-\d{2}-\d{2}\b',
            r'\b\d{1,2}[./-]\d{1,2}(?:[./-]\d{2,4})?\b',
            rf'\b\d{{1,2}}\s+(?:{months})(?:\s+\d{{4}})?\b',
            rf'\b(?:в|во)?\s*(?:следующ(?:ий|ую|ее)\s+)?(?:{weekdays})\b',
            r'\b(?:до конца|в конце|конец)\s+(?:недели|месяца)\b',
        )
        found = []
        for pattern in patterns:
            for match in re.finditer(pattern, value):
                phrase = match.group(0).strip()
                if phrase not in found:
                    found.append(phrase)
        return tuple(found)

    @classmethod
    def _recover_create_text(cls, message, proposal, pending):
        """Recover obvious record content omitted by a compact model.

        This is deliberately conservative: type-only and generic commands still
        reach policy as a missing text field and produce a clarification.
        """
        creating = proposal.action == Action.CREATE or (
            pending is not None and pending.intent.action == Action.CREATE
        )
        # A short clarification belongs to the field selected by the backend.
        # It must never be reused as record text by this generic recovery path.
        if (pending is not None and proposal.turn != TurnKind.NEW
                and cls._expected_field(pending) != 'text'):
            return proposal
        if not creating or proposal.text is not None or proposal.unknown_fields:
            return proposal
        candidate = message.strip()
        candidate = re.sub(
            r'^\s*(?:я\s+)?(?:хочу|планирую|собираюсь|нужно|надо)\s+',
            '', candidate, flags=re.IGNORECASE,
        )
        candidate = re.sub(
            r'^\s*(?:создай|добавь|запиши|поставь|сделай)\s+',
            '', candidate, flags=re.IGNORECASE,
        )
        for phrase in cls._date_phrases(message):
            candidate = re.sub(
                re.escape(phrase), ' ', candidate, flags=re.IGNORECASE,
            )
        for temporal in (proposal.date, proposal.target_date,
                         proposal.time, proposal.target_time):
            if temporal and temporal.evidence:
                candidate = re.sub(re.escape(temporal.evidence), ' ', candidate,
                                   flags=re.IGNORECASE)
        candidate = re.sub(r'\s+', ' ', candidate).strip(' .,;:!?—-')
        generic = {
            '', 'запись', 'задача', 'задачу', 'встреча', 'встречу',
            'напоминание', 'событие', 'это',
        }
        if candidate.lower().replace('ё', 'е') in generic:
            return proposal
        return proposal.model_copy(update={'text': candidate})

    @staticmethod
    def _normalize_action_fields(proposal, pending=None):
        updates = {}
        effective_action = proposal.action
        if (effective_action == Action.UNKNOWN and pending is not None):
            effective_action = pending.intent.action
        if effective_action == Action.CREATE:
            if proposal.text is None and proposal.target_query:
                updates.update({'text': proposal.target_query, 'target_query': None})
            if proposal.kind is None and proposal.target_kind:
                updates.update({'kind': proposal.target_kind, 'target_kind': None})
            if proposal.date is None and proposal.target_date:
                updates['date'] = proposal.target_date
            if proposal.time is None and proposal.target_time:
                updates['time'] = proposal.target_time
            updates.update({
                'target_query': None, 'target_kind': None,
                'target_temporal': 'unspecified',
                'target_date': None, 'target_time': None,
            })
        elif effective_action in (Action.READ, Action.CANCEL):
            if proposal.target_query is None and proposal.text:
                updates.update({'target_query': proposal.text, 'text': None})
            if proposal.target_kind is None and proposal.kind:
                updates.update({'target_kind': proposal.kind, 'kind': None})
            if proposal.target_date is None and proposal.date:
                updates['target_date'] = proposal.date
            if proposal.target_time is None and proposal.time:
                updates['target_time'] = proposal.time
            updates.update({
                'kind': None, 'text': None, 'date': None,
                'time': None, 'timezone': None,
                'duration_minutes': None, 'duration_description': None,
                'user_duration': None,
                'clear_fields': (),
            })
        return proposal.model_copy(update=updates) if updates else proposal

    @staticmethod
    def _pending_context(pending):
        if pending is None:
            return None
        return {
            'phase': pending.phase,
            'intent': pending.intent.model_dump(mode='json', exclude_unset=True),
            'candidate_count': len(pending.candidate_ids),
            'selected': pending.selected_record_id is not None,
            'question': pending.proposed_plan is not None,
            'expected_field': LLMOperationInterpreter._expected_field(pending),
        }

    @staticmethod
    def _expected_field(pending):
        if pending is None or pending.phase != Phase.NEED_FIELDS:
            return None
        if pending.intent.ambiguous_fields:
            return None
        if pending.proposed_plan is not None:
            record = pending.proposed_plan.after
            kind, text = record.kind, record.text
            day, clock = record.scheduled_date, record.scheduled_time
            duration_missing = (
                record.duration_status == 'missing'
                or record.duration.minutes is None
            )
        else:
            changes = pending.intent.changes
            kind, text = changes.kind, changes.text or ''
            day = changes.date_expression
            clock = changes.time_expression
            duration_missing = (
                changes.duration is None
                and 'duration' not in pending.intent.unknown_fields
            )
        if kind is None:
            return 'kind'
        if not text.strip():
            return 'text'
        if kind in ('meeting', 'reminder') and day is None:
            return 'date_expression'
        if kind in ('meeting', 'reminder') and clock is None:
            return 'time_expression'
        if kind == 'meeting' and duration_missing:
            return 'duration'
        return None

    @classmethod
    def _normalize_pending_answer(cls, message, pending, proposal):
        if (pending is not None and 'kind' in pending.intent.ambiguous_fields
                and cls._kind(proposal.kind) in {'meeting', 'reminder'}):
            selected_kind = cls._kind(proposal.kind)
            return proposal.model_copy(update={
                'turn': TurnKind.CONTINUE, 'action': Action.UNKNOWN,
                'kind': selected_kind, 'ambiguity_resolution': 'kind',
                'target_query': None, 'target_kind': None,
                'target_temporal': 'unspecified',
                'target_date': None, 'target_time': None,
                'text': None, 'date': None,
                'time': None, 'timezone': None,
                'duration_minutes': None, 'duration_description': None,
                'user_duration': None,
                'clear_fields': (),
                'read_fields': (), 'unknown_fields': (),
                'selected_candidate_number': None,
            })
        if (pending is not None and pending.intent.ambiguous_fields
                and proposal.ambiguity_resolution in pending.intent.ambiguous_fields):
            selected = proposal.ambiguity_resolution
            stored = pending.intent.ambiguity_values.get(selected)
            updates = {
                'turn': TurnKind.CONTINUE, 'action': Action.UNKNOWN,
                'target_query': None, 'target_kind': None,
                'target_temporal': 'unspecified',
                'target_date': None, 'target_time': None,
                'kind': None, 'text': None, 'date': None,
                'time': None, 'timezone': None,
                'duration_minutes': None, 'duration_description': None,
                'user_duration': None,
                'clear_fields': (), 'read_fields': (), 'unknown_fields': (),
                'selected_candidate_number': None,
            }
            if selected == 'duration' and stored:
                try:
                    updates['duration_minutes'] = int(stored)
                except ValueError:
                    updates['duration_description'] = stored
            elif selected in {'kind', 'text', 'date_expression',
                              'time_expression', 'timezone'} and stored:
                updates[selected] = stored
            return proposal.model_copy(update=updates)
        if (pending is not None and pending.phase == Phase.NEED_CONFIRMATION
                and proposal.request_alternative):
            return proposal.model_copy(update={
                'turn': TurnKind.CONTINUE, 'action': Action.UNKNOWN,
                'target_query': None, 'target_kind': None,
                'target_temporal': 'unspecified',
                'target_date': None, 'target_time': None,
                'kind': None, 'text': None, 'date': None,
                'time': None, 'timezone': None,
                'duration_minutes': None, 'duration_description': None,
                'user_duration': None,
                'clear_fields': (), 'read_fields': (), 'unknown_fields': (),
                'selected_candidate_number': None,
            })
        expected = cls._expected_field(pending)
        if expected is None or cls._looks_like_new_command(message):
            return proposal
        # expected_field is required now, but every explicit value in the
        # current patch survives for the backend merge engine.
        updates = {
            'turn': (TurnKind.CORRECTION if proposal.turn == TurnKind.CORRECTION
                     else TurnKind.CONTINUE),
            'action': Action.UNKNOWN,
            'target_query': None, 'target_kind': None,
            'target_temporal': 'unspecified',
            'target_date': None, 'target_time': None,
            'read_fields': (), 'request_alternative': False,
            'ambiguity_resolution': None, 'selected_candidate_number': None,
        }
        expected_fields = {
            'kind': ('kind',),
            'text': ('text',),
            'date_expression': ('date',),
            'time_expression': ('time',),
            'timezone': ('timezone',),
            'duration': ('duration_minutes', 'duration_description', 'user_duration'),
        }[expected]
        # Content/type/timezone are not generic temporal patch values. Keep
        # them only when the backend explicitly requested that field.
        if expected != 'kind':
            updates.update({'kind': None, 'kind_ambiguous': False})
        if expected != 'text':
            updates['text'] = None
        if expected != 'timezone':
            updates['timezone'] = None
        concrete = cls._proposal_has_value(proposal, expected)
        marks_expected_unknown = expected in proposal.unknown_fields
        if expected == 'duration' and cls._declines_duration(message):
            marks_expected_unknown = True
        if marks_expected_unknown and not concrete:
            for field in expected_fields:
                updates[field] = None
        updates['unknown_fields'] = (
            (expected,) if marks_expected_unknown and not concrete else ()
        )
        clear_name = {
            'date_expression': 'date', 'time_expression': 'time',
        }.get(expected, expected)
        updates['clear_fields'] = tuple(
            field for field in proposal.clear_fields if field == clear_name
        )
        return proposal.model_copy(update=updates)

    @staticmethod
    def _declines_duration(message):
        value = ' '.join(message.lower().replace('ё', 'е').split()).strip(' .,!')
        return bool(re.search(
            r'\b(?:не знаю|без понятия|неважно|не важно|оставь как есть|'
            r'не хочу уточнять|любой вариант|по умолчанию|хз)\b', value,
        ))

    @staticmethod
    def _proposal_has_value(proposal, field):
        if field == 'duration':
            return (proposal.duration_minutes is not None
                    or proposal.duration_description is not None
                    or proposal.user_duration is not None)
        return getattr(proposal, LLMOperationInterpreter._proposal_field(field)) is not None

    @staticmethod
    def _looks_like_new_command(message):
        value = message.lower().replace('ё', 'е')
        return bool(re.search(
            r'\b(?:создай|добавь|поставь|запиши|перенеси|измени|исправь|'
            r'отмени|удали|покажи|найди)\b',
            value,
        ))

    @staticmethod
    def _parse_proposal(arguments):
        raw = json.loads(arguments)
        if not isinstance(raw, dict):
            raise ValueError('proposal must be an object')
        if isinstance(raw.get('intent'), dict):
            intent = raw.pop('intent')
            for key, value in intent.items():
                raw.setdefault(key, value)
        target = raw.pop('target', None)
        if isinstance(target, dict):
            mapping = {
                'query': 'target_query', 'text': 'target_query',
                'kind': 'target_kind', 'temporal': 'target_temporal',
                'date_expression': 'target_date', 'time_expression': 'target_time',
                'date': 'target_date', 'time': 'target_time',
            }
            for key, output in mapping.items():
                if key in target:
                    raw.setdefault(output, target[key])
        changes = raw.pop('changes', None)
        if isinstance(changes, dict):
            duration = changes.pop('duration', None)
            if isinstance(duration, dict):
                if duration.get('type'):
                    raw.setdefault('user_duration', duration)
                else:
                    raw.setdefault('duration_minutes', duration.get('minutes'))
                    raw.setdefault('duration_description', duration.get('description'))
            for key, value in changes.items():
                key = {'date_expression': 'date',
                       'time_expression': 'time'}.get(key, key)
                raw.setdefault(key, value)
        patch = raw.pop('patch', None)
        if isinstance(patch, dict):
            duration = patch.pop('user_duration', patch.pop('duration', None))
            if isinstance(duration, dict):
                if duration.get('type'):
                    raw.setdefault('user_duration', duration)
                else:
                    raw.setdefault('duration_minutes', duration.get('minutes'))
                    raw.setdefault('duration_description', duration.get('description'))
            for key, value in patch.items():
                key = {'date_expression': 'date',
                       'time_expression': 'time'}.get(key, key)
                raw.setdefault(key, value)
        # Providers sometimes flatten the documented duration object instead
        # of placing it under changes. Normalize it before strict validation.
        duration = raw.pop('duration', None)
        if isinstance(duration, dict):
            if duration.get('type'):
                raw.setdefault('user_duration', duration)
            else:
                raw.setdefault('duration_minutes', duration.get('minutes'))
                raw.setdefault('duration_description', duration.get('description'))
        if raw.get('duration_type'):
            raw.setdefault('user_duration', {
                'type': raw.pop('duration_type'),
                'minutes': raw.pop('duration_minutes', None),
                'min_minutes': raw.pop('duration_min_minutes', None),
                'max_minutes': raw.pop('duration_max_minutes', None),
                'value': raw.pop('duration_value', None),
                'evidence': raw.pop('duration_evidence', None),
            })
        if 'requested_fields' in raw and 'read_fields' not in raw:
            raw['read_fields'] = raw.pop('requested_fields')
        for field in ('clear_fields', 'read_fields', 'unknown_fields'):
            if raw.get(field) is None:
                raw[field] = ()
        if raw.get('target_temporal') is None:
            raw['target_temporal'] = 'unspecified'
        for field in ('kind_ambiguous', 'request_alternative'):
            if raw.get(field) is None:
                raw[field] = False
        supplied_duration = raw.get('user_duration')
        if supplied_duration is not None:
            try:
                raw['user_duration'] = DurationInput.model_validate(
                    supplied_duration,
                ).model_dump(exclude_none=True)
            except (ValidationError, TypeError, ValueError):
                # An invalid optional duration must not make an otherwise valid
                # command unusable or become a fabricated user fact.
                raw.pop('user_duration', None)
        if 'selected_candidate' in raw and 'selected_candidate_number' not in raw:
            raw['selected_candidate_number'] = raw['selected_candidate']
        action_aliases = {
            'создать': 'create', 'создание': 'create',
            'изменить': 'update', 'изменение': 'update', 'перенести': 'update',
            'отменить': 'cancel', 'удалить': 'cancel',
            'посмотреть': 'read', 'найти': 'read', 'прочитать': 'read',
        }
        if isinstance(raw.get('action'), str):
            value = raw['action'].lower().replace('ё', 'е').strip()
            raw['action'] = action_aliases.get(value, value)
        turn_aliases = {
            'новая': 'new', 'новый': 'new', 'продолжение': 'continue',
            'подтверждение': 'confirm', 'отказ': 'reject', 'отмена': 'abandon',
            'исправление': 'correction', 'коррекция': 'correction',
        }
        if isinstance(raw.get('turn'), str):
            value = raw['turn'].lower().replace('ё', 'е').strip()
            raw['turn'] = turn_aliases.get(value, value)
        for field in ('date', 'target_date'):
            if field in raw:
                raw[field] = normalize_date_payload(raw[field])
        for field in ('time', 'target_time'):
            if field in raw:
                raw[field] = normalize_time_payload(raw[field])
        # Unknown provider-specific keys carry no authority. Dropping them is
        # safer and more compatible than failing the complete operation.
        allowed = set(Proposal.model_fields)
        raw = {key: value for key, value in raw.items() if key in allowed}
        try:
            return Proposal.model_validate(raw)
        except ValidationError as error:
            logger.warning(
                'Invalid proposal fields: %s',
                [(item['loc'], item['type']) for item in error.errors()],
            )
            raise

    @staticmethod
    def _kind(value):
        if value is None:
            return None
        normalized = value.lower().replace('ё', 'е').strip()
        aliases = {
            'task': 'task', 'задача': 'task', 'задачу': 'task',
            'meeting': 'meeting', 'встреча': 'meeting', 'встречу': 'meeting',
            'reminder': 'reminder', 'напоминание': 'reminder',
        }
        direct = aliases.get(normalized)
        if direct is not None:
            return direct
        # Compact models sometimes return an inflected Russian word or a broad
        # label such as "call"/"созвон". Only recognized domain kinds may
        # cross the adapter boundary. An unknown or ambiguous label means that
        # the kind is missing, so policy asks the user instead of returning 422.
        if re.fullmatch(r'задач\w*', normalized):
            return 'task'
        if re.fullmatch(r'встреч\w*', normalized):
            return 'meeting'
        if re.fullmatch(r'напомин\w*', normalized):
            return 'reminder'
        return None

    @staticmethod
    def _temporal(value):
        normalized = (value or 'unspecified').lower().strip()
        aliases = {
            'будущее': 'future', 'будущие': 'future',
            'прошлое': 'past', 'прошедшие': 'past',
            'все': 'all', 'не указано': 'unspecified',
        }
        return aliases.get(normalized, normalized)

