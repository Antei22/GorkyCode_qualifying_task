import json
import re
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.assistant.operation_interpreter import LLMOperationInterpreter, detect_explicit_kind
from app.application.operations.ports import TurnContext
from app.config import Settings
from app.domain.operations import (
    Action, OperationIntent, PendingOperation, Phase, RecordSnapshot, TurnKind,
    make_change_plan,
)
from app.domain.time import Clock


class FakeClient:
    def __init__(self, arguments, audit_arguments=None):
        self.arguments = arguments
        self.audit_arguments = audit_arguments or {'findings': []}
        self.chat = SimpleNamespace(completions=self)
        self.requests = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def create(self, **kwargs):
        self.requests.append(kwargs)
        name = kwargs['tools'][0]['function']['name']
        arguments = (self.audit_arguments if name == 'audit_operation'
                     else self.arguments)
        arguments = _structured_temporal_fixture(arguments)
        call = SimpleNamespace(function=SimpleNamespace(
            name=name, arguments=json.dumps(arguments),
        ))
        message = SimpleNamespace(tool_calls=[call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _structured_temporal_fixture(source):
    """Translate pre-v2 fixture payloads; production accepts only typed values."""
    data = json.loads(json.dumps(source))

    def parsed_date(value):
        if not isinstance(value, str):
            return value
        value = value.strip().lower()
        offsets = {'сегодня': 0, 'завтра': 1, 'послезавтра': 2, 'вчера': -1,
                   'tomorrow': 1}
        if value in offsets:
            return {'type': 'relative', 'days_offset': offsets[value]}
        weekdays = {'понедельник': 0, 'вторник': 1, 'среду': 2,
                    'среда': 2, 'четверг': 3, 'пятницу': 4,
                    'пятница': 4, 'субботу': 5, 'суббота': 5,
                    'воскресенье': 6}
        weekday = value.removeprefix('в ').removeprefix('во ')
        if weekday in weekdays:
            return {'type': 'weekday', 'weekday': weekdays[weekday],
                    'week_offset': 0}
        try:
            parsed = date.fromisoformat(value)
            return {'type': 'absolute', 'year': parsed.year,
                    'month': parsed.month, 'day': parsed.day}
        except ValueError:
            return None

    def parsed_time(value):
        if not isinstance(value, str):
            return value
        match = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?', value.strip())
        if not match:
            return None
        return {'hour': int(match.group(1)), 'minute': int(match.group(2) or 0)}

    def translate(container, mapping):
        for old, new in mapping.items():
            if old not in container:
                continue
            value = container.pop(old)
            converted = parsed_date(value) if 'date' in old else parsed_time(value)
            if converted is not None:
                container.setdefault(new, converted)

    translate(data, {'date_expression': 'date',
                     'target_date_expression': 'target_date',
                     'time_expression': 'time',
                     'target_time_expression': 'target_time'})
    if isinstance(data.get('target'), dict):
        translate(data['target'], {'date_expression': 'date',
                                   'time_expression': 'time'})
    if isinstance(data.get('changes'), dict):
        translate(data['changes'], {'date_expression': 'date',
                                    'time_expression': 'time'})
    for finding in data.get('findings', []):
        if finding.get('field') == 'date_expression' and 'date' not in finding:
            parsed = parsed_date(finding.get('value'))
            if parsed is not None:
                finding['date'] = parsed
        if finding.get('field') == 'time_expression' and 'time' not in finding:
            parsed = parsed_time(finding.get('value'))
            if parsed is not None:
                finding['time'] = parsed
    return data


def context(message):
    return TurnContext(
        owner_id=uuid4(), request_id=uuid4(), message=message,
        now=Clock(fixed_now='2026-10-28T12:00:00+03:00').now(),
    )


class InterpreterTests(unittest.TestCase):
    def interpreter(self, arguments, audit_arguments=None):
        client = FakeClient(arguments, audit_arguments)
        adapter = LLMOperationInterpreter(
            Settings(llm_api_key='test'), lambda **kwargs: client,
        )
        return adapter, client

    def test_structured_update_separates_target_and_changes(self):
        adapter, client = self.interpreter({
            'turn': 'new', 'action': 'update',
            'target_query': 'встреча с Марией',
            'target_date_expression': 'завтра',
            'date_expression': 'в пятницу',
            'time_expression': '18:00',
            'duration_minutes': 120,
        })
        result = adapter.interpret(
            context('Перенеси встречу с Марией с завтра на пятницу в 18 на два часа'),
            None,
        )
        self.assertEqual(result.intent.action, Action.UPDATE)
        self.assertEqual(result.intent.target.date_expression, '2026-10-29')
        self.assertEqual(result.intent.changes.date_expression, '2026-10-30')
        self.assertEqual(result.intent.changes.duration.minutes, 120)
        self.assertEqual(len(client.requests), 1)

    def test_model_cannot_invent_numeric_duration(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create',
            'kind': 'meeting', 'text': 'Встреча', 'duration_minutes': 90,
        })
        result = adapter.interpret(context('Создай встречу'), None)
        self.assertIsNone(result.intent.changes.duration)

    def test_candidate_number_is_mapped_by_server(self):
        first, second = uuid4(), uuid4()
        now = context('вторую').now
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_TARGET,
            intent=OperationIntent(action=Action.UPDATE),
            candidate_ids=(first, second), created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        adapter, _ = self.interpreter({
            'turn': 'continue', 'action': 'unknown',
            'selected_candidate_number': 2,
        })
        result = adapter.interpret(context('вторую'), pending)
        self.assertEqual(result.selected_candidate_id, second)

    def test_confirmation_is_deterministic_and_does_not_call_model(self):
        now = context('да').now
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_CONFIRMATION,
            intent=OperationIntent(action=Action.UPDATE),
            created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        adapter = LLMOperationInterpreter(Settings(llm_api_key=''))
        result = adapter.interpret(context('да'), pending)
        self.assertEqual(result.turn, TurnKind.CONFIRM)

    def test_complete_action_without_pending_is_normalized_to_new(self):
        adapter, _ = self.interpreter({
            'turn': 'continue', 'action': 'update',
            'target_query': 'задача проверить систему',
            'text': 'Проверить единый механизм',
        })
        result = adapter.interpret(context('Переименуй задачу'), None)
        self.assertEqual(result.turn, TurnKind.NEW)

    def test_nested_compact_model_output_is_safely_normalized(self):
        adapter, _ = self.interpreter({
            'turn': 'continue',
            'intent': {
                'action': 'update',
                'target': {'text': 'старая задача', 'kind': 'задача'},
                'changes': {'text': 'новая задача'},
            },
        })
        result = adapter.interpret(context('Переименуй старую задачу'), None)
        self.assertEqual(result.turn, TurnKind.NEW)
        self.assertEqual(result.intent.target.query, 'старая задача')
        self.assertEqual(result.intent.target.kind, 'task')
        self.assertIsNone(result.intent.changes.text)

    def test_call_semantics_select_meeting_and_keep_collected_time(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'kind_ambiguous': True,
            'text': 'Созвониться с Иваном', 'date_expression': 'завтра',
            'time_expression': '16:00',
        })
        first_context = context('Хочу завтра созвониться с Иваном в 4 часа')
        first = adapter.interpret(first_context, None)
        self.assertEqual(first.intent.changes.kind, 'meeting')
        self.assertEqual(first.intent.semantic_class, 'meeting_like')
        self.assertEqual(first.intent.ambiguous_fields, ())
        self.assertIn('созвониться', first.intent.changes.text.lower())
        self.assertEqual(first.intent.changes.date_expression, '2026-10-29')
        self.assertEqual(first.intent.changes.time_expression, '16:00')

    def test_call_semantics_override_unsupported_model_kind(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'call',
            'text': 'созвониться с Иваном', 'date_expression': 'завтра',
        })
        result = adapter.interpret(
            context('Хочу завтра созвониться с Иваном'), None,
        )
        self.assertEqual(result.intent.changes.kind, 'meeting')
        self.assertEqual(result.intent.semantic_class, 'meeting_like')

    def test_inflected_kind_is_normalized(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'встречей',
            'text': 'обсудить проект',
        })
        result = adapter.interpret(context('Создай встречу'), None)
        self.assertEqual(result.intent.changes.kind, 'meeting')

    def test_explicit_meeting_outweighs_weak_modal_words(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'task',
            'text': 'назначить встречу с Иваном',
        })
        result = adapter.interpret(
            context('Мне нужно назначить встречу с Иваном'), None,
        )
        self.assertEqual(result.intent.changes.kind, 'meeting')
        self.assertEqual(result.intent.semantic_class, 'meeting_like')
        self.assertEqual(result.intent.kind_candidates, ())

    def test_explicit_reminder_command_overrides_meeting_like_text(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'text': 'позвонить Ивану',
            'date': {'type': 'relative', 'days_offset': 1, 'evidence': 'завтра'},
            'time': {'type': 'absolute', 'hour': 10, 'minute': 0,
                     'evidence': '10 утра'},
        })
        result = adapter.interpret(
            context('Напомни мне завтра в 10 утра позвонить Ивану'), None,
        )
        self.assertEqual(result.intent.changes.kind, 'reminder')
        self.assertEqual(result.intent.kind_source, 'explicit_user_command')
        self.assertEqual(result.intent.changes.text, 'позвонить Ивану')

    def test_explicit_reminder_command_covers_meeting_like_verbs(self):
        for message in (
            'Напомни мне созвониться с Иваном завтра',
            'Напомни мне встретиться с преподавателем в пятницу',
        ):
            detection = detect_explicit_kind(message)
            self.assertIsNotNone(detection)
            self.assertEqual(detection.kind, 'reminder')
            self.assertEqual(detection.source, 'explicit_user_command')

    def test_explicit_entity_commands_set_kind_source(self):
        self.assertEqual(
            detect_explicit_kind('Создай напоминание позвонить маме завтра').kind,
            'reminder',
        )
        self.assertEqual(
            detect_explicit_kind('Поставь задачу позвонить Ивану').kind,
            'task',
        )
        self.assertEqual(
            detect_explicit_kind('Назначь встречу с Иваном завтра').kind,
            'meeting',
        )

    def test_weak_call_intent_stays_task_reminder_ambiguous(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'task',
            'text': 'позвонить Ивану',
            'date': {'type': 'relative', 'days_offset': 1, 'evidence': 'завтра'},
        })
        result = adapter.interpret(
            context('Мне надо позвонить Ивану завтра'), None,
        )
        self.assertIsNone(result.intent.changes.kind)
        self.assertEqual(result.intent.kind_candidates, ('task', 'reminder'))

    def test_planned_meeting_outweighs_nado(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'task',
            'text': 'запланировать встречу',
            'date': {'type': 'relative', 'days_offset': 1, 'evidence': 'завтра'},
        })
        result = adapter.interpret(
            context('Надо запланировать встречу на завтра'), None,
        )
        self.assertEqual(result.intent.changes.kind, 'meeting')
        self.assertEqual(result.intent.semantic_class, 'meeting_like')

    def test_todo_like_report_has_task_reminder_candidates(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'task',
            'text': 'отправить отчет',
            'date': {'type': 'weekday', 'weekday': 4, 'week_offset': 0,
                     'evidence': 'В пятницу'},
        })
        result = adapter.interpret(
            context('В пятницу мне нужно отправить отчет'), None,
        )
        self.assertIsNone(result.intent.changes.kind)
        self.assertEqual(result.intent.semantic_class, 'todo_like')
        self.assertEqual(result.intent.kind_candidates, ('task', 'reminder'))
        self.assertEqual(result.intent.ambiguous_fields, ('kind',))

    def test_todo_like_tickets_has_task_reminder_candidates(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'task',
            'text': 'купить билеты',
            'temporal': {'type': 'period', 'unit': 'week', 'offset': 0,
                         'mode': 'until_end', 'evidence': 'до конца недели'},
        })
        result = adapter.interpret(
            context('Надо купить билеты до конца недели'), None,
        )
        self.assertIsNone(result.intent.changes.kind)
        self.assertEqual(result.intent.semantic_class, 'todo_like')
        self.assertEqual(result.intent.kind_candidates, ('task', 'reminder'))

    def test_kind_choice_preserves_todo_semantic_class(self):
        now = context('напоминание').now
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_FIELDS,
            intent=OperationIntent(
                action=Action.CREATE,
                semantic_class='todo_like',
                kind_candidates=('task', 'reminder'),
                changes={
                    'text': 'отправить отчет',
                    'date_expression': '2026-10-30',
                },
                ambiguous_fields=('kind',),
            ),
            created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        adapter, _ = self.interpreter({
            'turn': 'continue', 'action': 'unknown', 'kind': 'reminder',
        })
        result = adapter.interpret(context('напоминание'), pending)
        self.assertEqual(result.intent.changes.kind, 'reminder')
        self.assertEqual(result.intent.semantic_class, 'todo_like')
        self.assertEqual(result.intent.kind_candidates, ())

    def test_numeric_absolute_date_is_normalized_to_canonical_iso(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'text': 'встретиться с Алексеем',
            'date': {'type': 'absolute', 'year': 2026, 'month': 11, 'day': 20,
                     'evidence': '20.11.2026'},
            'time': {'hour': 10, 'minute': 0, 'evidence': 'в 10:00'},
        })
        result = adapter.interpret(
            context('Назначь встречу с мамой в 10:00 на 20.11.2026'), None,
        )
        self.assertEqual(result.intent.changes.date_expression, '2026-11-20')
        self.assertEqual(result.intent.changes.time_expression, '10:00')
        self.assertEqual(
            result.intent.field_evidence['date_expression'], '20.11.2026',
        )
        self.assertEqual(
            result.intent.field_evidence['time_expression'], 'в 10:00',
        )
        self.assertEqual(
            result.intent.source_text,
            'Назначь встречу с мамой в 10:00 на 20.11.2026',
        )

    def test_invalid_model_date_is_dropped_when_user_gave_no_date(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'text': 'встретиться с Алексеем',
            'date_expression': 'someday',
        })
        result = adapter.interpret(
            context('Хочу встретиться с Алексеем'), None,
        )
        self.assertNotIn('date_expression', result.intent.changes.model_fields_set)

    def test_audit_recovers_explicit_time_omitted_by_first_pass(self):
        adapter, client = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'text': 'встреча с Марией', 'date_expression': 'завтра',
        }, {
            'findings': [{
                'field': 'time_expression', 'disposition': 'explicit',
                'value': '16:30', 'evidence': 'в 16:30',
            }],
        })
        result = adapter.interpret(
            context('Создай встречу с Марией на завтра в 16:30'), None,
        )
        self.assertEqual(result.intent.changes.time_expression, '16:30')
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(
            client.requests[1]['tools'][0]['function']['name'], 'audit_operation',
        )

    def test_audit_rejects_value_without_source_evidence(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'text': 'встреча с Марией', 'date_expression': 'завтра',
        }, {
            'findings': [{
                'field': 'time_expression', 'disposition': 'explicit',
                'value': '18:00', 'evidence': 'в 18:00',
            }],
        })
        result = adapter.interpret(
            context('Создай встречу с Марией на завтра в 16:30'), None,
        )
        self.assertNotIn(
            'time_expression', result.intent.changes.model_fields_set,
        )

    def test_server_keeps_time_duration_collision_ambiguous(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'text': 'встреча с Марией', 'date_expression': 'завтра',
            'duration_minutes': 240,
        }, {
            'findings': [{
                # Even an explicit choice by the audit model cannot settle a
                # phrase that has two roles without a contextual cue.
                'field': 'duration', 'disposition': 'explicit',
                'value': '240', 'evidence': 'на 4 часа',
            }],
        })
        result = adapter.interpret(
            context('Создай встречу с Марией завтра на 4 часа'), None,
        )
        self.assertEqual(
            set(result.intent.ambiguous_fields),
            {'time_expression', 'duration'},
        )
        self.assertIsNone(result.intent.changes.duration)
        self.assertNotIn('time_expression', result.intent.changes.model_fields_set)
        self.assertEqual(result.intent.ambiguity_values['duration'], '240')
        self.assertEqual(result.intent.ambiguity_values['time_expression'], '4')

    def test_ambiguity_role_selection_reuses_stored_value(self):
        now = context('Длительность').now
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_FIELDS,
            intent=OperationIntent(
                action=Action.CREATE,
                changes={
                    'kind': 'meeting', 'text': 'встреча с Марией',
                    'date_expression': 'завтра',
                },
                ambiguous_fields=('duration', 'time_expression'),
                ambiguity_values={'duration': '240', 'time_expression': '4'},
                ambiguity_evidence='на 4 часа',
            ),
            created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        adapter, _ = self.interpreter({
            'turn': 'continue', 'action': 'unknown',
            'ambiguity_resolution': 'duration',
        })
        result = adapter.interpret(context('Длительность'), pending)
        self.assertEqual(result.intent.changes.duration.minutes, 240)
        self.assertEqual(result.intent.resolved_ambiguity, 'duration')
        self.assertNotIn('text', result.intent.changes.model_fields_set)

    def test_explicit_duration_cue_resolves_time_duration_collision(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'text': 'встреча с Марией', 'date_expression': 'завтра',
            'duration_minutes': 240,
        })
        result = adapter.interpret(
            context('Назначь встречу с Марией завтра длительностью на 4 часа'), None,
        )
        self.assertEqual(result.intent.ambiguous_fields, ())
        self.assertEqual(result.intent.changes.duration.minutes, 240)

    def test_day_part_resolves_time_duration_collision_as_start_time(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'text': 'встреча с Марией', 'date_expression': 'завтра',
            'time_expression': '16:00',
        })
        result = adapter.interpret(
            context('Назначь встречу с Марией завтра на 4 часа дня'), None,
        )
        self.assertEqual(result.intent.ambiguous_fields, ())
        self.assertEqual(result.intent.changes.time_expression, '16:00')

    def test_missing_create_text_is_recovered_from_specific_request(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'date_expression': 'завтра',
        })
        result = adapter.interpret(
            context('Хочу встретиться с Алексеем завтра'), None,
        )
        self.assertEqual(
            result.intent.changes.text, 'встретиться с Алексеем',
        )

    def test_generic_create_command_does_not_become_record_text(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
        })
        result = adapter.interpret(context('Создай встречу'), None)
        self.assertNotIn('text', result.intent.changes.model_fields_set)

    def test_pending_create_can_recover_text_from_clarification(self):
        now = context('Встреча с Алексеем').now
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_FIELDS,
            intent=OperationIntent(
                action=Action.CREATE,
                changes={'kind': 'meeting', 'date_expression': 'завтра'},
            ),
            created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        adapter, _ = self.interpreter({
            'turn': 'continue', 'action': 'unknown',
        })
        result = adapter.interpret(context('Встреча с Алексеем'), pending)
        self.assertEqual(result.intent.changes.text, 'Встреча с Алексеем')

    def test_meet_intention_is_meeting_like(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'kind_ambiguous': True,
            'text': 'встретиться с Алексеем', 'date_expression': 'завтра',
        })
        result = adapter.interpret(
            context('Хочу встретиться с Алексеем завтра'), None,
        )
        self.assertEqual(result.intent.changes.kind, 'meeting')
        self.assertEqual(result.intent.semantic_class, 'meeting_like')
        self.assertEqual(result.intent.ambiguous_fields, ())

    def test_server_semantic_priority_overrides_kind_audit_ambiguity(self):
        adapter, client = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'text': 'увидеться с Виталием', 'date_expression': 'завтра',
        }, {
            'findings': [{
                'field': 'kind', 'disposition': 'ambiguous',
                'evidence': 'Хочу увидеться с Виталием',
            }],
        })
        result = adapter.interpret(
            context('Хочу увидеться с Виталием завтра'), None,
        )
        self.assertEqual(result.intent.changes.kind, 'meeting')
        self.assertEqual(result.intent.semantic_class, 'meeting_like')
        self.assertEqual(result.intent.ambiguous_fields, ())
        self.assertEqual(len(client.requests), 1)

    def test_short_time_answer_cannot_change_pending_action(self):
        now = context('На 4 часа').now
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_FIELDS,
            intent=OperationIntent(
                action=Action.CREATE,
                changes={
                    'kind': 'meeting', 'text': 'встретиться с Алексеем',
                    'date_expression': 'завтра',
                },
            ),
            proposed_plan=make_change_plan(
                'create', None,
                RecordSnapshot(
                    id=uuid4(), kind='meeting', text='встретиться с Алексеем',
                    scheduled_date=now.date() + timedelta(days=1),
                    scheduled_time=None, timezone='Europe/Moscow',
                    status='open', revision='new',
                ),
            ),
            created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'update', 'duration_minutes': 240,
            'time_expression': '16:00',
        })
        result = adapter.interpret(context('На 4 часа'), pending)
        self.assertEqual(result.turn, TurnKind.CONTINUE)
        self.assertEqual(result.intent.action, Action.UNKNOWN)
        self.assertEqual(result.intent.changes.time_expression, '16:00')
        self.assertEqual(result.intent.changes.duration.minutes, 240)

    def test_date_follow_up_keeps_date_and_time_from_one_answer(self):
        now = context('На завтра в 10 утра').now
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_FIELDS,
            intent=OperationIntent(
                action=Action.CREATE,
                changes={'kind': 'reminder', 'text': 'увидеться с Саней'},
            ),
            proposed_plan=make_change_plan(
                'create', None,
                RecordSnapshot(
                    id=uuid4(), kind='reminder', text='увидеться с Саней',
                    scheduled_date=None, scheduled_time=None,
                    timezone='Europe/Moscow', status='open', revision='new',
                ),
            ),
            created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        adapter, _ = self.interpreter({
            'turn': 'continue', 'action': 'unknown',
            'patch': {
                'date': {
                    'type': 'relative', 'year': None,
                    'resolution': 'nearest_future', 'evidence': 'завтра',
                },
                'time': {
                    'type': 'absolute', 'hour': 10, 'minute': 0,
                    'evidence': 'в 10 утра',
                },
            },
        })
        result = adapter.interpret(context('На завтра в 10 утра'), pending)
        self.assertEqual(result.turn, TurnKind.CONTINUE)
        self.assertEqual(result.intent.changes.date_expression, '2026-10-29')
        self.assertEqual(result.intent.changes.time_expression, '10:00')

    def test_tool_schema_explains_contextual_colloquial_time(self):
        adapter, client = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'task', 'text': 'test',
        })
        adapter.interpret(context('Создай задачу test'), None)
        schema = client.requests[0]['tools'][0]['function']['parameters']
        self.assertIn('date', schema['properties'])
        self.assertNotIn('date_expression', schema['properties'])
        description = schema['properties']['time']['description']
        self.assertIn('на 4 дня', description)
        system = client.requests[0]['messages'][0]['content']
        self.assertIn('expected_field=time_expression', system)

    def test_unknown_duration_uses_general_llm_contract(self):
        now = context('хз').now
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_FIELDS,
            intent=OperationIntent(
                action=Action.CREATE,
                changes={
                    'kind': 'meeting', 'text': 'встретиться с Алексеем',
                    'date_expression': 'завтра', 'time_expression': '16:00',
                },
            ),
            proposed_plan=make_change_plan(
                'create', None,
                RecordSnapshot(
                    id=uuid4(), kind='meeting', text='встретиться с Алексеем',
                    scheduled_date=now.date() + timedelta(days=1),
                    scheduled_time=now.time().replace(
                        hour=16, minute=0, second=0, microsecond=0, tzinfo=None,
                    ),
                    timezone='Europe/Moscow', status='open', revision='new',
                ),
            ),
            created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        adapter, client = self.interpreter({
            'turn': 'continue', 'action': 'unknown',
            'unknown_fields': ['duration'],
            'text': 'хз', 'target_query': 'ошибочная цель',
        })
        result = adapter.interpret(context('хз'), pending)
        self.assertEqual(result.turn, TurnKind.CONTINUE)
        self.assertEqual(result.intent.action, Action.UNKNOWN)
        self.assertEqual(result.intent.unknown_fields, ('duration',))
        self.assertIsNone(result.intent.changes.duration)
        self.assertEqual(result.intent.changes.model_fields_set, set())
        self.assertEqual(result.intent.target.model_fields_set, set())
        self.assertEqual(len(client.requests), 1)

    def test_duration_answer_cannot_overwrite_collected_text(self):
        now = context('час-два').now
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_FIELDS,
            intent=OperationIntent(
                action=Action.CREATE,
                changes={
                    'kind': 'meeting', 'text': 'встреча с мамой',
                    'date_expression': '2026-10-30',
                    'time_expression': '10:00',
                },
            ),
            proposed_plan=make_change_plan(
                'create', None,
                RecordSnapshot(
                    id=uuid4(), kind='meeting', text='встреча с мамой',
                    scheduled_date=now.date() + timedelta(days=2),
                    scheduled_time=now.time().replace(
                        hour=10, minute=0, second=0, microsecond=0, tzinfo=None,
                    ),
                    timezone='Europe/Moscow', status='open', revision='new',
                ),
            ),
            created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        adapter, _ = self.interpreter({
            'turn': 'continue', 'action': 'unknown',
            'text': 'час-два', 'duration_description': 'час-два',
            'target_query': 'ошибочная цель',
        })
        result = adapter.interpret(context('час-два'), pending)
        self.assertEqual(result.turn, TurnKind.CONTINUE)
        self.assertEqual(result.intent.changes.model_fields_set, {'duration'})
        self.assertEqual(result.intent.changes.duration.type, 'range')
        self.assertEqual(result.intent.changes.duration.min_minutes, 60)
        self.assertEqual(result.intent.changes.duration.max_minutes, 120)
        self.assertEqual(result.intent.changes.duration.evidence, 'час-два')
        self.assertNotIn('text', result.intent.changes.model_fields_set)
        self.assertEqual(result.intent.target.model_fields_set, set())

    def test_duration_question_cannot_search_with_question_words(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'read',
            'target_query': 'Сколько будет длится встреча с мамой',
            'target_kind': 'meeting', 'target_temporal': 'future',
            'requested_fields': [],
        })
        result = adapter.interpret(
            context('Сколько будет длится встреча с мамой'), None,
        )
        self.assertEqual(result.intent.target.query, 'встреча с мамой')
        self.assertEqual(result.intent.target.kind, 'meeting')
        self.assertEqual(result.intent.target.temporal, 'future')
        self.assertEqual(result.intent.read_fields, ('duration',))

    def test_invalid_optional_duration_and_provider_extras_do_not_break_create(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'text': 'встреча с мамой',
            'date': {'type': 'absolute', 'year': 2026, 'month': 10, 'day': 30,
                     'evidence': '30.10.26'},
            'time': {'type': 'absolute', 'hour': 10, 'minute': 0,
                     'evidence': '10'},
            'user_duration': {'type': 'exact', 'minutes': None},
            'provider_comment': 'not part of the contract',
            'requested_fields': None,
        })
        result = adapter.interpret(
            context('Создай встречу с мамой в 10 30.10.26'), None,
        )
        self.assertEqual(result.intent.action, Action.CREATE)
        self.assertEqual(result.intent.changes.text, 'встреча с мамой')
        self.assertNotIn('duration', result.intent.changes.model_fields_set)

    def test_create_continuation_cannot_pollute_target_query(self):
        now = context('завтра').now
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_FIELDS,
            intent=OperationIntent(
                action=Action.CREATE,
                changes={'kind': 'reminder', 'text': 'Позвонить Ивану'},
            ),
            proposed_plan=make_change_plan(
                'create', None,
                RecordSnapshot(
                    id=uuid4(), kind='reminder', text='Позвонить Ивану',
                    scheduled_date=None, scheduled_time=None,
                    timezone='Europe/Moscow', status='open', revision='new',
                ),
            ),
            created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        adapter, _ = self.interpreter({
            'turn': 'continue', 'action': 'unknown',
            'target_date_expression': 'завтра',
        })
        result = adapter.interpret(context('завтра'), pending)
        self.assertEqual(result.intent.changes.date_expression, '2026-10-29')
        self.assertEqual(result.intent.target.model_fields_set, set())

    def test_concrete_value_supersedes_repeated_unknown_marker(self):
        now = context('в 10').now
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_FIELDS,
            intent=OperationIntent(
                action=Action.CREATE,
                changes={
                    'kind': 'reminder', 'text': 'Позвонить Ивану',
                    'date_expression': 'завтра',
                },
                unknown_fields=('time_expression',),
            ),
            proposed_plan=make_change_plan(
                'create', None,
                RecordSnapshot(
                    id=uuid4(), kind='reminder', text='Позвонить Ивану',
                    scheduled_date=now.date() + timedelta(days=1),
                    scheduled_time=None, timezone='Europe/Moscow',
                    status='open', revision='new',
                ),
            ),
            created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        adapter, _ = self.interpreter({
            'turn': 'continue', 'action': 'unknown',
            'time_expression': '10:00',
            'unknown_fields': ['time_expression'],
        })
        result = adapter.interpret(context('в 10'), pending)
        self.assertEqual(result.intent.changes.time_expression, '10:00')
        self.assertEqual(result.intent.unknown_fields, ())

    def test_request_for_another_slot_continues_confirmation(self):
        now = context('Покажи другое время').now
        plan = make_change_plan(
            'create', None,
            RecordSnapshot(
                id=uuid4(), kind='meeting', text='Встреча',
                scheduled_date=now.date() + timedelta(days=1),
                scheduled_time=now.time().replace(
                    hour=17, minute=0, second=0, microsecond=0, tzinfo=None,
                ),
                duration={'minutes': 60, 'source': 'user'},
                timezone='Europe/Moscow', status='open', revision='new',
            ),
        )
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_CONFIRMATION,
            intent=OperationIntent(
                action=Action.CREATE,
                changes={
                    'kind': 'meeting', 'text': 'Встреча',
                    'date_expression': '2026-10-29',
                    'time_expression': '17:00',
                    'duration': {'minutes': 60},
                },
            ),
            proposed_plan=plan, created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'read',
            'target_query': 'свободное время',
            'request_alternative': True,
        })
        result = adapter.interpret(
            context('Мне неудобно, какое ещё время есть?'), pending,
        )
        self.assertEqual(result.turn, TurnKind.UNKNOWN)
        self.assertIsNone(result.intent)
        self.assertEqual(result.parser_used, 'confirmation_resolver')
        self.assertEqual(result.confirmation_decision, 'unknown')

    def test_confirmation_state_never_auto_confirms_target_text(self):
        now = context('встреча с Дашей').now
        record_id = uuid4()
        plan = make_change_plan(
            'update',
            RecordSnapshot(
                id=record_id, kind='meeting', text='встреча с Дашей',
                scheduled_date=now.date(), scheduled_time=now.time().replace(
                    second=0, microsecond=0, tzinfo=None,
                ), timezone='Europe/Moscow', status='open', revision='1',
            ),
            RecordSnapshot(
                id=record_id, kind='meeting', text='встреча с Дашей',
                scheduled_date=now.date() + timedelta(days=2),
                scheduled_time=now.time().replace(
                    second=0, microsecond=0, tzinfo=None,
                ), timezone='Europe/Moscow', status='open', revision='1',
            ),
        )
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_CONFIRMATION,
            intent=OperationIntent(action=Action.UPDATE), proposed_plan=plan,
            created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        adapter, client = self.interpreter({
            'turn': 'confirm', 'action': 'update',
            'patch': {'date': {'type': 'relative', 'days_offset': 2,
                               'evidence': 'послезавтра'}},
        })
        unknown = adapter.interpret(context('встреча с Дашей'), pending)
        self.assertEqual(unknown.turn, TurnKind.UNKNOWN)
        self.assertEqual(unknown.confirmation_decision, 'unknown')
        self.assertEqual(len(client.requests), 0)
        confirmed = adapter.interpret(context('да'), pending)
        self.assertEqual(confirmed.turn, TurnKind.CONFIRM)
        self.assertEqual(confirmed.confirmation_decision, 'confirm')

    def test_list_guard_fixes_tomorrow_and_applies_meeting_kind(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'read',
            'date': {
                'type': 'weekday', 'weekday': 1, 'week_offset': 0,
                'evidence': 'завтра',
            },
            'requested_fields': ['date', 'time', 'text'],
        })
        result = adapter.interpret(
            context('Есть ли у меня встречи завтра?'), None,
        )
        self.assertEqual(result.intent.action, Action.LIST)
        self.assertEqual(result.intent.target.kinds, ('meeting',))
        self.assertEqual(result.intent.target.date_expression, '2026-10-29')

    def test_list_kind_guards_cover_tasks_reminders_and_all_types(self):
        cases = (
            ('Какие задачи завтра?', 'task'),
            ('Какие напоминания сегодня?', 'reminder'),
            ('Что запланировано завтра?', None),
        )
        for message, expected_kind in cases:
            evidence = 'сегодня' if 'сегодня' in message.lower() else 'завтра'
            adapter, _ = self.interpreter({
                'turn': 'new', 'action': 'read',
                'date': {
                    'type': 'relative',
                    'days_offset': 0 if evidence == 'сегодня' else 1,
                    'evidence': evidence,
                },
            })
            result = adapter.interpret(context(message), None)
            self.assertEqual(result.intent.action, Action.LIST)
            self.assertEqual(
                result.intent.target.kinds,
                (expected_kind,) if expected_kind else (),
            )

    def test_single_update_date_cannot_be_duplicated_into_target(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'update',
            'target_query': 'встреча с Дашей',
            'date': {'type': 'weekday', 'weekday': 4, 'week_offset': 0},
            'target_date': {'type': 'weekday', 'weekday': 4, 'week_offset': 0},
        })
        result = adapter.interpret(
            context('Перенеси встречу с Дашей на послезавтра'), None,
        )
        self.assertEqual(result.intent.changes.date_expression, '2026-10-30')
        self.assertNotIn('date_expression', result.intent.target.model_fields_set)

    def test_until_end_period_gets_backend_mode_and_current_evidence(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'task',
            'date': {'type': 'period', 'unit': 'week'},
        })
        result = adapter.interpret(
            context('Поставь задачу купить билеты до конца недели'), None,
        )
        self.assertEqual(result.intent.changes.text, 'купить билеты')
        self.assertEqual(result.intent.changes.temporal.mode, 'until_end')
        self.assertEqual(
            result.intent.changes.temporal.evidence, 'до конца недели',
        )


if __name__ == '__main__':
    unittest.main()
