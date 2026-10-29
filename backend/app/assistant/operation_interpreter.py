"""Structured LLM adapter. It proposes intent and never executes a mutation."""
import json
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from openai import OpenAI

from app.domain.duration import explicit_duration
from app.domain.models import DomainError
from app.domain.time import parse_day
from app.domain.operations import (
    Action, CalendarScope, Interpretation, OperationField, OperationIntent, PendingOperation,
    Phase, RecordPatch, TargetQuery, TurnKind,
)
from app.observability import pipeline_event


class Proposal(BaseModel):
    model_config = ConfigDict(extra='ignore')
    turn: TurnKind = TurnKind.UNKNOWN
    action: Action = Action.UNKNOWN
    target_query: str | None = None
    target_kind: str | None = None
    target_temporal: str = 'unspecified'
    target_date_expression: str | None = None
    target_time_expression: str | None = None
    kind: str | None = None
    kind_ambiguous: bool = Field(
        default=False,
        description=(
            'Пользователь выразил намерение выполнить действие, но не выбрал, '
            'создать само событие/встречу или напоминание о нём.'
        ),
    )
    text: str | None = None
    date_expression: str | None = None
    time_expression: str | None = Field(
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
    clear_fields: tuple[Literal['kind', 'text', 'date', 'time', 'timezone',
                                'duration'], ...] = ()
    read_fields: tuple[str, ...] = ()
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
    calendar_scope: CalendarScope | None = Field(
        default=None,
        description=(
            'Режим поиска альтернативного времени: work_hours — будни 08:00–18:00; '
            'weekdays_extended_hours — будни 08:00–22:00; all_days_work_hours — '
            'любой день 08:00–18:00; extended_hours — любой день 08:00–22:00; '
            'weekends_work_hours — выходные 08:00–18:00; '
            'weekends_extended_hours — выходные 08:00–22:00.'
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


class ConfirmationProposal(BaseModel):
    """Narrow model output while the server is waiting for plan confirmation."""
    model_config = ConfigDict(extra='forbid')
    decision: Literal[
        'confirm', 'reject', 'alternative', 'new_operation', 'unclear',
    ]
    calendar_scope: CalendarScope | None = None


class AuditFinding(BaseModel):
    """One source-grounded addition or ambiguity found after extraction."""
    model_config = ConfigDict(extra='forbid')
    field: OperationField
    disposition: Literal['explicit', 'ambiguous']
    value: str | None = None
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
Поля с префиксом target_ описывают СТАРУЮ запись; поля без префикса — только
желаемое новое состояние. text — содержание создаваемой записи или новый текст.
Для "с завтра на пятницу": завтра относится к target.date_expression, пятница к
changes.date_expression. Не придумывай отсутствующие дату, время, тип и минуты.
Встреча и напоминание — разные типы. Если пользователь лишь выражает намерение
созвониться, увидеться, встретиться или выполнить синонимичное действие, но явно
не называет тип записи, оставь kind пустым и верни kind_ambiguous=true. Сервер
должен уточнить: создать само событие/встречу или напоминание о действии.
Для вопроса "сколько будет длиться" используй
read, read_fields=["duration"], target_temporal=future; "сколько длилась" — past.
Если пользователь явно просит убрать поле, добавь его имя в clear_fields; обычное
отсутствие поля не означает очистку.
Если пользователь прямо сообщает, что НЕ ЗНАЕТ значение поля (например, "хз",
"не знаю", "без понятия"), добавь ожидаемое поле в unknown_fields. Это применимо
к любому полю, не только к длительности. Не оставляй такой ответ просто пустым.
Текущая операция содержит expected_field — это смысл вопроса, на который отвечает
новая реплика. Если expected_field=time_expression, разговорные ответы «на 4 дня»,
«в 4 дня», «на 4 часа дня» означают время начала 16:00. Не относись к ним как к
duration. Если expected_field=duration, та же числовая конструкция может означать
длительность. Контекст ожидаемого поля важнее изолированного прочтения фразы.
Если система предложила свободное время и ждёт подтверждения, а пользователь просит
другое время (например, «мне неудобно, какое ещё есть?», «покажи следующий вариант»),
верни turn=continue, action=unknown и request_alternative=true. Это продолжение
текущей операции, не read/update и не новая команда.
Если expected_field=calendar_scope, выбери сочетание дней и часов:
work_hours — будни 08:00–18:00; weekdays_extended_hours — будни 08:00–22:00;
all_days_work_hours — любой день 08:00–18:00; extended_hours — любой день
08:00–22:00; weekends_work_hours — только выходные 08:00–18:00;
weekends_extended_hours — только выходные 08:00–22:00.
Например, «в будни в любое время» — weekdays_extended_hours,
«можно и в выходные, но только днём» — all_days_work_hours,
«могу только в выходные» — weekends_work_hours. Это только настройка
серверного поиска: не меняй поля записи и верни turn=continue, action=unknown.
Если текущая операция содержит ambiguous_fields и пользователь выбирает одну из
предложенных ролей (например, отвечает «длительность»), верни выбранное имя поля в
ambiguity_resolution. Не проси его повторять исходное значение: оно хранится сервером.
Если пользователь выбирает вариант из показанного списка, верни его номер, не UUID.
Одна реплика описывает одну операцию. Обязательно вызови propose_operation.'''


AUDIT_INSTRUCTIONS = '''Проверь структурированный разбор пользовательской реплики.
Ищи только явно присутствующие, но пропущенные значения полей, а также выражения,
роль которых неоднозначна. Не заполняй поле только потому, что оно обязательно.
Для каждого finding приведи точную непрерывную цитату evidence из новой реплики.
disposition=explicit допустим только при единственной смысловой роли; value содержит
извлечённое значение. disposition=ambiguous используй, если одна цитата может означать
разные поля; укажи второе поле в ambiguous_with и не придумывай value.
Учитывай expected_field текущего диалога: ответ на явный вопрос о времени относится
к time_expression, ответ на вопрос о длительности — к duration. Уже заполненные поля
не повторяй, кроме случая, когда их роль на самом деле неоднозначна.
Если первичный разбор выбрал meeting/reminder, хотя пользователь лишь выразил
намерение и не назвал тип записи, отметь kind как ambiguous.
Если новых или неоднозначных значений нет, верни пустой findings.'''


CONFIRMATION_INSTRUCTIONS = '''Определи только реакцию пользователя на предложенный
сервером план. Ничего не выполняй и не разбирай поля записи.
decision=confirm — пользователь принимает предложенный план;
decision=reject — пользователь отказывается от текущего плана;
decision=alternative — предложенное время не подходит и нужен другой вариант;
decision=new_operation — пользователь начал отдельную самостоятельную команду;
decision=unclear — смысл ответа не позволяет выбрать один вариант.
Если пользователь отклоняет только предложенное время и одновременно уточняет,
когда он доступен, это alternative, не reject. Заполни calendar_scope:
work_hours — будни 08:00–18:00; weekdays_extended_hours — будни 08:00–22:00;
all_days_work_hours — любой день 08:00–18:00; extended_hours — любой день 08:00–22:00;
weekends_work_hours — только выходные 08:00–18:00;
weekends_extended_hours — только выходные 08:00–22:00.
Если меняются только дни, сохрани диапазон часов из текущей операции.
Обязательно вызови resolve_confirmation.'''


class LLMOperationInterpreter:
    def __init__(self, settings, client_factory=OpenAI):
        self.settings = settings
        self.client_factory = client_factory

    def interpret(self, context, pending: PendingOperation | None) -> Interpretation:
        deterministic = self._turn(context.message, pending)
        if deterministic is not None:
            pipeline_event(
                'interpreter', 'deterministic_turn', context,
                turn=deterministic.turn,
                reason='short confirmation/rejection/abandonment matched by server',
            )
            return deterministic
        if not self.settings.llm_api_key:
            raise DomainError(
                'Для чата настройте LLM_API_KEY, LLM_BASE_URL и LLM_MODEL в .env.'
            )
        if pending is not None and pending.phase == Phase.NEED_CONFIRMATION:
            confirmation = self._interpret_confirmation(context, pending)
            if confirmation is not None:
                return confirmation
            # resolve_confirmation classified this as an independent command.
            # The full parser must not see or merge with the abandoned draft.
            pending = None
        pending_context = self._pending_context(pending)
        history = '\n'.join(f'{role}: {content}' for role, content in context.history[-8:])
        messages = [
            {'role': 'system', 'content': INSTRUCTIONS},
            {'role': 'user', 'content': (
                f'Сейчас: {context.now.isoformat()}\n'
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
        pipeline_event(
            'llm', 'request_sent', context,
            model=self.settings.llm_model, function='propose_operation',
            pending_context=pending_context, history_messages=min(len(context.history), 8),
        )
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
        pipeline_event(
            'llm', 'response_received', context,
            model=getattr(response, 'model', self.settings.llm_model),
            finish_reason=getattr(response.choices[0], 'finish_reason', None),
            tool_calls=[call.function.name for call in calls],
            arguments=(calls[0].function.arguments if len(calls) == 1 else None),
        )
        if len(calls) != 1 or calls[0].function.name != 'propose_operation':
            raise DomainError('Модель не смогла однозначно разобрать операцию.')
        try:
            proposal = self._parse_proposal(calls[0].function.arguments)
        except (ValueError, TypeError, ValidationError) as error:
            raise DomainError('Модель вернула некорректный разбор операции.') from error
        proposal = self._normalize_action_fields(proposal, pending)
        proposal = self._normalize_pending_answer(context.message, pending, proposal)
        pipeline_event(
            'interpreter', 'proposal_normalized', context,
            proposal=proposal,
        )
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
            proposal = self._verify_dates(context, proposal)
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
            if proposal.target_date_expression is not None:
                target_values['date_expression'] = proposal.target_date_expression
            if proposal.target_time_expression is not None:
                target_values['time_expression'] = proposal.target_time_expression
            target = TargetQuery(**target_values)
            change_values = {key: value for key, value in {
                    'kind': self._kind(proposal.kind),
                    'text': proposal.text,
                    'date_expression': proposal.date_expression,
                    'time_expression': proposal.time_expression,
                    'timezone': proposal.timezone,
                    'duration': ({'minutes': proposal.duration_minutes,
                                  'description': proposal.duration_description}
                                 if proposal.duration_minutes is not None
                                 or proposal.duration_description is not None else None),
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
            calendar_scope=proposal.calendar_scope,
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
        interpretation = Interpretation(
            turn=turn, intent=intent, selected_candidate_id=selected,
        )
        pipeline_event(
            'interpreter', 'intent_ready', context,
            turn=turn, intent=intent, selected_candidate_id=selected,
        )
        return interpretation

    def _interpret_confirmation(self, context, pending):
        """Classify a confirmation turn without exposing operation fields."""
        pending_context = self._pending_context(pending)
        history = '\n'.join(
            f'{role}: {content}' for role, content in context.history[-8:]
        )
        messages = [
            {'role': 'system', 'content': CONFIRMATION_INSTRUCTIONS},
            {'role': 'user', 'content': (
                f'Сейчас: {context.now.isoformat()}\n'
                f'Текущая операция: {json.dumps(pending_context, ensure_ascii=False)}\n'
                f'Недавний диалог:\n{history}\n'
                f'Новая реплика: {context.message}'
            )},
        ]
        function_name = 'resolve_confirmation'
        specification = {'type': 'function', 'function': {
            'name': function_name,
            'description': 'Классифицировать ответ на предложенный сервером план.',
            'parameters': ConfirmationProposal.model_json_schema(),
        }}
        pipeline_event(
            'llm', 'request_sent', context,
            model=self.settings.llm_model, function=function_name,
            pending_context=pending_context,
            history_messages=min(len(context.history), 8),
        )
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
                tool_choice={
                    'type': 'function', 'function': {'name': function_name},
                },
                temperature=0,
            )
        calls = response.choices[0].message.tool_calls or []
        pipeline_event(
            'llm', 'response_received', context,
            model=getattr(response, 'model', self.settings.llm_model),
            finish_reason=getattr(response.choices[0], 'finish_reason', None),
            tool_calls=[call.function.name for call in calls],
            arguments=(calls[0].function.arguments if len(calls) == 1 else None),
        )
        if len(calls) != 1 or calls[0].function.name != function_name:
            raise DomainError('Модель не смогла однозначно разобрать подтверждение.')
        try:
            proposal = ConfirmationProposal.model_validate_json(
                calls[0].function.arguments,
            )
        except (ValueError, TypeError, ValidationError) as error:
            raise DomainError(
                'Модель вернула некорректный разбор подтверждения.'
            ) from error
        pipeline_event(
            'interpreter', 'confirmation_classified', context,
            decision=proposal.decision, calendar_scope=proposal.calendar_scope,
        )
        explicit_scope = self._confirmation_calendar_scope(
            context.message, pending.intent.calendar_scope,
        )
        if explicit_scope is not None:
            proposal = proposal.model_copy(update={
                'decision': 'alternative', 'calendar_scope': explicit_scope,
            })
        if proposal.decision == 'confirm':
            return Interpretation(turn=TurnKind.CONFIRM)
        if proposal.decision == 'reject':
            return Interpretation(turn=TurnKind.REJECT)
        if proposal.decision == 'alternative':
            return Interpretation(
                turn=TurnKind.CONTINUE,
                intent=OperationIntent(
                    alternative_requested=True,
                    calendar_scope=proposal.calendar_scope,
                ),
            )
        if proposal.decision == 'unclear':
            # A no-op continuation makes policy repeat the current confirmation
            # without discarding the server-owned pending plan.
            return Interpretation(
                turn=TurnKind.CONTINUE, intent=OperationIntent(),
            )
        # A genuine new command is parsed by the full operation schema below.
        return None

    @staticmethod
    def _confirmation_calendar_scope(message, current_scope):
        """Extract an explicit availability correction from a confirmation turn."""
        value = message.lower().replace('ё', 'е')
        mentions_weekends = bool(re.search(r'\bвыходн\w*\b', value))
        mentions_weekdays = bool(re.search(r'\bбудн\w*\b', value))
        weekends_only = bool(re.search(
            r'только\s+(?:в\s+)?выходн\w*', value,
        ))
        weekdays_only = bool(re.search(
            r'только\s+(?:по\s+|в\s+)?будн\w*', value,
        ))
        # During confirmation, a negative correction changes the selected set of
        # days. It is different from «можно и в выходные», which broadens it.
        correction = bool(re.search(r'\b(?:нет|передумал\w*)\b', value))
        cannot_weekdays = bool(re.search(
            r'не\s+могу[^.!?]*\bбудн\w*\b', value,
        ))
        cannot_weekends = bool(re.search(
            r'не\s+могу[^.!?]*\bвыходн\w*\b', value,
        ))
        can_weekends = bool(re.search(
            r'(?<!не\s)могу[^.!?]*\bвыходн\w*\b', value,
        ))
        can_weekdays = bool(re.search(
            r'(?<!не\s)могу[^.!?]*\bбудн\w*\b', value,
        ))
        if (can_weekends and (cannot_weekdays or correction)
                or correction and mentions_weekends and not mentions_weekdays):
            weekends_only = True
        if (can_weekdays and (cannot_weekends or correction)
                or correction and mentions_weekdays and not mentions_weekends):
            weekdays_only = True
        if not weekends_only and not weekdays_only:
            return None
        explicitly_extended = bool(re.search(
            r'(?:вечер|до\s*22|в любое время|не только рабоч)', value,
        ))
        explicitly_work = bool(re.search(
            r'(?:рабочее время|до\s*18|только.*дн)', value,
        ))
        if explicitly_extended and not explicitly_work:
            extended = True
        elif explicitly_work and not explicitly_extended:
            extended = False
        else:
            extended = current_scope in {
                'weekdays_extended_hours', 'extended_hours',
                'weekends_extended_hours',
            }
        if weekends_only:
            return ('weekends_extended_hours' if extended
                    else 'weekends_work_hours')
        return ('weekdays_extended_hours' if extended else 'work_hours')

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
        if (proposal.date_expression is None
                and proposal.target_date_expression is None
                and cls._date_phrases(message)):
            return True
        clock_like = re.search(
            r'\b(?:[01]?\d|2[0-3]):[0-5]\d\b|'
            r'\b(?:в|на)\s+\d{1,2}(?:\s+час\w*)?(?:\s+(?:дня|вечера|утра|ночи))?\b',
            value,
        )
        if clock_like and (proposal.time_expression is None
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
        pipeline_event(
            'llm_audit', 'request_sent', context,
            model=self.settings.llm_model, function='audit_operation',
            primary_proposal=proposal,
        )
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
        pipeline_event(
            'llm_audit', 'response_received', context,
            model=getattr(response, 'model', self.settings.llm_model),
            finish_reason=getattr(response.choices[0], 'finish_reason', None),
            tool_calls=[call.function.name for call in calls],
            arguments=(calls[0].function.arguments if len(calls) == 1 else None),
        )
        if len(calls) != 1 or calls[0].function.name != 'audit_operation':
            return proposal, (), {}, None
        try:
            audit = AuditResult.model_validate_json(calls[0].function.arguments)
        except (ValueError, TypeError, ValidationError):
            return proposal, (), {}, None
        pipeline_event(
            'llm_audit', 'findings_validated', context,
            findings=audit.findings,
        )
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
            if finding.value is None:
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
            if getattr(proposal, field) is not None:
                continue
            if field == 'kind':
                normalized = cls._kind(finding.value)
                if normalized is not None:
                    updates['kind'] = normalized
            elif field == 'text':
                if cls._evidence_present(message, finding.value):
                    updates['text'] = finding.value
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
                    updates[field] = None
        return (proposal.model_copy(update=updates) if updates else proposal,
                tuple(sorted(ambiguous)), ambiguity_values, ambiguity_evidence)

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
        if (pending is not None
                and LLMOperationInterpreter._expected_field(pending) == 'calendar_scope'):
            weekdays = bool(re.search(r'\bбудн\w*\b', value))
            weekends = bool(re.search(r'\bвыходн\w*\b', value))
            weekends_only = bool(re.search(
                r'только\s+(?:в\s+)?выходн\w*', value,
            ))
            extended = bool(re.search(
                r'(?:вечер|до\s*22|в любое время|не только рабоч|расшир)', value,
            ))
            work_only = bool(re.search(
                r'(?:только.*рабоч|рабочее время|до\s*18|только.*дн)', value,
            ))
            if weekdays and extended:
                return Interpretation(
                    turn=TurnKind.CONTINUE,
                    intent=OperationIntent(calendar_scope='weekdays_extended_hours'),
                )
            if weekends_only:
                return Interpretation(
                    turn=TurnKind.CONTINUE,
                    intent=OperationIntent(
                        calendar_scope=('weekends_work_hours' if work_only
                                        else 'weekends_extended_hours'),
                    ),
                )
            if weekends and work_only:
                return Interpretation(
                    turn=TurnKind.CONTINUE,
                    intent=OperationIntent(calendar_scope='all_days_work_hours'),
                )
            if weekdays or work_only:
                return Interpretation(
                    turn=TurnKind.CONTINUE,
                    intent=OperationIntent(calendar_scope='work_hours'),
                )
            if weekends or extended or re.search(r'^можно\b', value):
                return Interpretation(
                    turn=TurnKind.CONTINUE,
                    intent=OperationIntent(calendar_scope='extended_hours'),
                )
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
        if proposal.duration_minutes is None:
            return proposal
        actual = explicit_duration(message)
        if actual == proposal.duration_minutes:
            return proposal
        return proposal.model_copy(update={'duration_minutes': None})

    @classmethod
    def _verify_dates(cls, context, proposal):
        """Keep a model date only if the server can parse it.

        If a compact model returns a verbose or translated value, recover the
        single explicit date phrase from the user's own message. Multiple date
        phrases are deliberately not guessed (for example, move from/to).
        """
        candidates = cls._date_phrases(context.message)
        updates = {}
        if len(candidates) == 1:
            if (proposal.action == Action.CREATE
                    and proposal.date_expression is None):
                updates['date_expression'] = candidates[0]
            elif (proposal.action in (Action.READ, Action.CANCEL)
                    and proposal.target_date_expression is None):
                updates['target_date_expression'] = candidates[0]
        for field in ('date_expression', 'target_date_expression'):
            expression = getattr(proposal, field)
            if expression is None:
                continue
            try:
                parse_day(expression, context.now)
            except DomainError:
                updates[field] = candidates[0] if len(candidates) == 1 else None
        verified = proposal.model_copy(update=updates) if updates else proposal
        if verified.target_date_expression is not None:
            try:
                target_day = parse_day(
                    verified.target_date_expression, context.now,
                )
            except DomainError:
                target_day = None
            if target_day is not None:
                temporal = (
                    'past' if target_day < context.now.date()
                    else 'future' if target_day > context.now.date()
                    else 'unspecified'
                )
                if verified.target_temporal != temporal:
                    verified = verified.model_copy(update={
                        'target_temporal': temporal,
                    })
        return verified

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
            r'\b\d{1,2}\.\d{1,2}\.\d{4}\b',
            r'\b\d{1,2}(?:-го)?\s+числа\b',
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
        # A short answer to a pending date/time/duration question is field data,
        # never a replacement title.  The existing title is already stored in
        # pending.intent and will be preserved by merge_intent.
        if pending is not None and cls._expected_field(pending) != 'text':
            return proposal
        creating = proposal.action == Action.CREATE or (
            pending is not None and pending.intent.action == Action.CREATE
        )
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
        if proposal.time_expression:
            candidate = re.sub(
                re.escape(proposal.time_expression), ' ', candidate,
                flags=re.IGNORECASE,
            )
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
            if proposal.date_expression is None and proposal.target_date_expression:
                updates['date_expression'] = proposal.target_date_expression
            if proposal.time_expression is None and proposal.target_time_expression:
                updates['time_expression'] = proposal.target_time_expression
            updates.update({
                'target_query': None, 'target_kind': None,
                'target_temporal': 'unspecified',
                'target_date_expression': None, 'target_time_expression': None,
            })
        elif effective_action in (Action.READ, Action.CANCEL):
            if proposal.target_query is None and proposal.text:
                updates.update({'target_query': proposal.text, 'text': None})
            if proposal.target_kind is None and proposal.kind:
                updates.update({'target_kind': proposal.kind, 'kind': None})
            if proposal.target_date_expression is None and proposal.date_expression:
                updates['target_date_expression'] = proposal.date_expression
            if proposal.target_time_expression is None and proposal.time_expression:
                updates['target_time_expression'] = proposal.time_expression
            updates.update({
                'kind': None, 'text': None, 'date_expression': None,
                'time_expression': None, 'timezone': None,
                'duration_minutes': None, 'duration_description': None,
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
            duration = record.duration.minutes
        else:
            changes = pending.intent.changes
            kind, text = changes.kind, changes.text or ''
            day = changes.date_expression
            clock = changes.time_expression
            duration = changes.duration.minutes if changes.duration else None
        if kind is None:
            return 'kind'
        if not text.strip():
            return 'text'
        if kind in ('meeting', 'reminder') and day is None:
            return 'date_expression'
        if kind in ('meeting', 'reminder') and clock is None:
            return 'time_expression'
        if kind == 'meeting' and duration is None:
            return 'duration'
        if (pending.proposed_plan is not None
                and pending.intent.calendar_scope is None):
            return 'calendar_scope'
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
                'target_date_expression': None, 'target_time_expression': None,
                'text': None, 'date_expression': None,
                'time_expression': None, 'timezone': None,
                'duration_minutes': None, 'duration_description': None,
                'clear_fields': (('date', 'time')
                                 if selected_kind == 'reminder' else ()),
                'read_fields': (), 'unknown_fields': (),
                'calendar_scope': None,
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
                'target_date_expression': None, 'target_time_expression': None,
                'kind': None, 'text': None, 'date_expression': None,
                'time_expression': None, 'timezone': None,
                'duration_minutes': None, 'duration_description': None,
                'clear_fields': (), 'read_fields': (), 'unknown_fields': (),
                'calendar_scope': None,
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
                'target_date_expression': None, 'target_time_expression': None,
                'kind': None, 'text': None, 'date_expression': None,
                'time_expression': None, 'timezone': None,
                'duration_minutes': None, 'duration_description': None,
                'clear_fields': (), 'read_fields': (), 'unknown_fields': (),
                'selected_candidate_number': None,
            })
        expected = cls._expected_field(pending)
        if expected is None or cls._looks_like_new_command(message):
            return proposal
        if expected == 'calendar_scope':
            return proposal.model_copy(update={
                'turn': TurnKind.CONTINUE, 'action': Action.UNKNOWN,
                'target_query': None, 'target_kind': None,
                'target_temporal': 'unspecified',
                'target_date_expression': None, 'target_time_expression': None,
                'kind': None, 'text': None, 'date_expression': None,
                'time_expression': None, 'timezone': None,
                'duration_minutes': None, 'duration_description': None,
                'clear_fields': (), 'read_fields': (), 'unknown_fields': (),
                'request_alternative': False,
                'ambiguity_resolution': None,
                'selected_candidate_number': None,
            })
        updates = {'turn': TurnKind.CONTINUE, 'action': Action.UNKNOWN}
        # Compact models sometimes copy a short clarification into `text`.
        # Conversation state is authoritative: while another field is expected,
        # such a value must not overwrite an already collected record title.
        if expected != 'text':
            updates['text'] = None
        # A number followed by "часа" is a duration in isolation, but it is an
        # appointment time when answering the explicit question "На какое время?".
        if expected == 'time_expression':
            updates['duration_minutes'] = None
            updates['duration_description'] = None
        elif expected == 'duration':
            updates['time_expression'] = None
        # The expected field is authoritative conversation state. Reject a
        # model attempt to mark some unrelated field as unknown.
        if proposal.unknown_fields:
            concrete = cls._proposal_has_value(proposal, expected)
            marks_expected_unknown = expected in proposal.unknown_fields
            # A concrete value from the new turn always supersedes an old
            # unknown marker that the model copied from pending context.
            updates['unknown_fields'] = (
                (expected,) if marks_expected_unknown and not concrete else ()
            )
            if marks_expected_unknown and not concrete:
                # An explicit "unknown" answer carries no new field value. It
                # must never overwrite already collected data with the literal
                # reply (for example, text="не знаю").
                updates.update({
                    'target_query': None, 'target_kind': None,
                    'target_temporal': 'unspecified',
                    'target_date_expression': None,
                    'target_time_expression': None,
                    'kind': None, 'text': None, 'date_expression': None,
                    'time_expression': None, 'timezone': None,
                    'duration_minutes': None, 'duration_description': None,
                    'clear_fields': (), 'read_fields': (),
                })
        return proposal.model_copy(update=updates)

    @staticmethod
    def _proposal_has_value(proposal, field):
        if field == 'duration':
            return (proposal.duration_minutes is not None
                    or proposal.duration_description is not None)
        return getattr(proposal, field) is not None

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
                'date_expression': 'target_date_expression',
                'time_expression': 'target_time_expression',
            }
            for key, output in mapping.items():
                if key in target:
                    raw.setdefault(output, target[key])
        changes = raw.pop('changes', None)
        if isinstance(changes, dict):
            duration = changes.pop('duration', None)
            if isinstance(duration, dict):
                raw.setdefault('duration_minutes', duration.get('minutes'))
                raw.setdefault('duration_description', duration.get('description'))
            for key, value in changes.items():
                raw.setdefault(key, value)
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
        }
        if isinstance(raw.get('turn'), str):
            value = raw['turn'].lower().replace('ё', 'е').strip()
            raw['turn'] = turn_aliases.get(value, value)
        return Proposal.model_validate(raw)

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

