import json
import unittest
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.assistant.operation_interpreter import LLMOperationInterpreter
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
        call = SimpleNamespace(function=SimpleNamespace(
            name=name, arguments=json.dumps(arguments),
        ))
        message = SimpleNamespace(tool_calls=[call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


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
        self.assertEqual(result.intent.target.date_expression, 'завтра')
        self.assertEqual(result.intent.changes.date_expression, 'в пятницу')
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
        self.assertEqual(result.intent.changes.text, 'новая задача')

    def test_ambiguous_call_keeps_type_open_and_reminder_clears_event_time(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'kind_ambiguous': True,
            'text': 'Созвониться с Иваном', 'date_expression': 'завтра',
            'time_expression': '16:00',
        })
        first_context = context('Хочу завтра созвониться с Иваном в 4 часа')
        first = adapter.interpret(first_context, None)
        self.assertIsNone(first.intent.changes.kind)
        self.assertEqual(first.intent.ambiguous_fields, ('kind',))
        self.assertIn('созвониться', first.intent.changes.text.lower())

        now = first_context.now
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_FIELDS, intent=first.intent,
            created_at=now, updated_at=now,
            expires_at=now + timedelta(minutes=30),
        )
        second_adapter, _ = self.interpreter({
            'turn': 'continue', 'action': 'unknown', 'kind': 'reminder',
        })
        second = second_adapter.interpret(context('Напоминание'), pending)
        self.assertEqual(second.intent.changes.kind, 'reminder')
        self.assertIsNone(second.intent.changes.date_expression)
        self.assertIn('date_expression', second.intent.changes.model_fields_set)
        self.assertEqual(second.intent.target.model_fields_set, set())

    def test_unsupported_model_kind_becomes_missing_field(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'call',
            'text': 'созвониться с Иваном', 'date_expression': 'завтра',
        })
        result = adapter.interpret(
            context('Хочу завтра созвониться с Иваном'), None,
        )
        self.assertIsNone(result.intent.changes.kind)
        self.assertNotIn('kind', result.intent.changes.model_fields_set)

    def test_inflected_kind_is_normalized(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'встречей',
            'text': 'обсудить проект',
        })
        result = adapter.interpret(context('Создай встречу'), None)
        self.assertEqual(result.intent.changes.kind, 'meeting')

    def test_verbose_model_date_is_recovered_from_user_message(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'text': 'встретиться с Алексеем',
            'date_expression': 'tomorrow at some point',
        })
        result = adapter.interpret(
            context('Хочу встретиться с Алексеем завтра'), None,
        )
        self.assertEqual(result.intent.changes.date_expression, 'завтра')

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

    def test_meet_intention_keeps_type_open(self):
        adapter, _ = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'meeting',
            'kind_ambiguous': True,
            'text': 'встретиться с Алексеем', 'date_expression': 'завтра',
        })
        result = adapter.interpret(
            context('Хочу встретиться с Алексеем завтра'), None,
        )
        self.assertIsNone(result.intent.changes.kind)
        self.assertEqual(result.intent.ambiguous_fields, ('kind',))

    def test_audit_detects_synonymous_kind_ambiguity(self):
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
        self.assertIsNone(result.intent.changes.kind)
        self.assertEqual(result.intent.ambiguous_fields, ('kind',))
        self.assertEqual(len(client.requests), 2)

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
        self.assertIsNone(result.intent.changes.duration)

    def test_tool_schema_explains_contextual_colloquial_time(self):
        adapter, client = self.interpreter({
            'turn': 'new', 'action': 'create', 'kind': 'task', 'text': 'test',
        })
        adapter.interpret(context('Создай задачу test'), None)
        schema = client.requests[0]['tools'][0]['function']['parameters']
        description = schema['properties']['time_expression']['description']
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
        self.assertEqual(result.intent.changes.date_expression, 'завтра')
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
        self.assertEqual(result.turn, TurnKind.CONTINUE)
        self.assertEqual(result.intent.action, Action.UNKNOWN)
        self.assertTrue(result.intent.alternative_requested)
        self.assertEqual(result.intent.target.model_fields_set, set())
        self.assertEqual(result.intent.changes.model_fields_set, set())


if __name__ == '__main__':
    unittest.main()
