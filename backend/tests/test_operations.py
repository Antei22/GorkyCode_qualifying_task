import unittest
from datetime import date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.application.operations import (
    DefaultChangePlanner, DefaultOperationPolicy, DefaultOutcomePresenter,
    OperationCoordinator,
)
from app.application.operations.codec import dump_pending, load_pending
from app.application.operations.ports import TurnContext
from app.application.operations.state import merge_intent, merge_patch
from app.domain.operations import (
    Action, DurationInput, DurationValue, ExecutionResult, Interpretation,
    OperationIntent, PendingOperation, Phase, RecordPatch, RecordSnapshot,
    TargetQuery, TargetResolution, TurnKind, make_change_plan,
)


NOW = datetime.fromisoformat('2026-10-28T12:00:00+03:00')
OWNER = UUID('00000000-0000-0000-0000-000000000001')
REQUEST = UUID('00000000-0000-0000-0000-000000000002')
OPERATION = UUID('00000000-0000-0000-0000-000000000003')


def snapshot(text='Встреча с Марией', day=date(2026, 10, 29),
             clock=time(16), revision='1', record_id=None, kind='meeting',
             duration=60, status='open'):
    return RecordSnapshot(
        id=record_id or uuid4(), kind=kind, text=text,
        scheduled_date=day, scheduled_time=clock, timezone='Europe/Moscow',
        duration=DurationValue(minutes=duration, source='user'),
        status=status, revision=revision,
    )


class Interpreter:
    def __init__(self, *values):
        self.values = iter(values)

    def interpret(self, context, pending):
        return next(self.values)


class Resolver:
    def __init__(self, candidates=()):
        self.candidates = tuple(candidates)
        self.calls = []

    def resolve(self, context, intent, selected_id=None):
        self.calls.append((intent, selected_id))
        values = self.candidates
        if selected_id is not None:
            values = tuple(item for item in values if item.id == selected_id)
        return TargetResolution(candidates=values)


class Planner:
    def __init__(self):
        self.calls = []

    def prepare(self, context, intent, target, previous_plan=None):
        self.calls.append((intent, target))
        base = target or (previous_plan.after if previous_plan else snapshot(
            text='', day=None, clock=None, duration=None,
            kind=None, record_id=uuid4(), revision='new'))
        values = base.model_dump()
        patch = intent.changes
        fields = patch.model_fields_set
        if 'kind' in fields:
            values['kind'] = patch.kind
        if 'text' in fields:
            values['text'] = patch.text or ''
        if 'date_expression' in fields:
            values['scheduled_date'] = (date.fromisoformat(patch.date_expression)
                                        if patch.date_expression else None)
        if 'time_expression' in fields:
            values['scheduled_time'] = (time.fromisoformat(patch.time_expression)
                                        if patch.time_expression else None)
        if 'timezone' in fields:
            values['timezone'] = patch.timezone
        if 'duration' in fields:
            supplied = patch.duration or DurationInput()
            values['duration'] = DurationValue(
                minutes=supplied.minutes, description=supplied.description,
                source='user' if supplied.minutes is not None else 'unknown',
            )
        if intent.action == Action.CANCEL:
            values['status'] = 'cancelled'
        after = RecordSnapshot.model_validate(values)
        return make_change_plan(intent.action.value, target, after)


class Executor:
    def __init__(self):
        self.plans = []

    def execute(self, context, plan):
        self.plans.append(plan)
        return ExecutionResult(action=plan.action, records=(plan.after,))


def context(now=NOW):
    return TurnContext(owner_id=OWNER, request_id=REQUEST,
                       message='test', now=now)


def coordinator(interpreter, resolver=None, planner=None, executor=None, policy=None):
    return OperationCoordinator(
        interpreter=interpreter,
        resolver=resolver or Resolver(),
        planner=planner or Planner(),
        policy=policy or DefaultOperationPolicy(),
        executor=executor or Executor(),
        operation_id_factory=lambda: OPERATION,
    )


class OperationStateTests(unittest.TestCase):
    def test_patch_merge_keeps_safe_fields_and_collects_conflict(self):
        previous = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(
                kind='meeting', text='Встреча', time_expression='10:00',
            ),
        )
        current = OperationIntent(changes=RecordPatch(
            date_expression='2026-10-29', time_expression='11:00',
            duration=DurationInput(minutes=60),
        ))
        merged, accepted, same, conflicts = merge_patch(previous, current)
        self.assertEqual(
            accepted, ('date_expression', 'duration'),
        )
        self.assertEqual(same, ())
        self.assertEqual(merged.changes.date_expression, '2026-10-29')
        self.assertEqual(merged.changes.duration.minutes, 60)
        self.assertEqual(merged.changes.time_expression, '10:00')
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].field, 'time_expression')
        self.assertEqual(conflicts[0].new_value, '11:00')

    def test_patch_merge_treats_equal_value_as_same_and_correction_overwrites(self):
        previous = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(time_expression='10:00'),
        )
        equal, accepted, same, conflicts = merge_patch(
            previous,
            OperationIntent(changes=RecordPatch(time_expression='10:00')),
        )
        self.assertEqual(equal.changes.time_expression, '10:00')
        self.assertEqual(accepted, ())
        self.assertEqual(same, ('time_expression',))
        self.assertEqual(conflicts, ())

        corrected, accepted, same, conflicts = merge_patch(
            previous,
            OperationIntent(changes=RecordPatch(time_expression='11:00')),
            allow_overwrite=True,
        )
        self.assertEqual(corrected.changes.time_expression, '11:00')
        self.assertEqual(accepted, ('time_expression',))
        self.assertEqual(same, ())
        self.assertEqual(conflicts, ())

    def test_merge_preserves_omitted_fields_and_explicit_null(self):
        first = OperationIntent(
            action=Action.UPDATE,
            changes=RecordPatch(text='Новое название', time_expression='16:00'),
        )
        merged = merge_intent(
            first,
            OperationIntent(changes=RecordPatch(time_expression=None)),
        )
        self.assertEqual(merged.changes.text, 'Новое название')
        self.assertIsNone(merged.changes.time_expression)
        self.assertEqual(merged.changes.model_fields_set, {'text', 'time_expression'})

    def test_unknown_field_is_distinct_and_concrete_answer_supersedes_it(self):
        first = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(kind='meeting', text='Встреча'),
            unknown_fields=('time_expression',),
        )
        unknown = merge_intent(
            first,
            OperationIntent(
                changes=RecordPatch(time_expression='16:00'),
            ),
        )
        self.assertEqual(unknown.unknown_fields, ())
        self.assertEqual(unknown.changes.time_expression, '16:00')

        replaced = merge_intent(
            unknown,
            OperationIntent(unknown_fields=('time_expression',)),
        )
        self.assertEqual(replaced.unknown_fields, ('time_expression',))
        self.assertNotIn('time_expression', replaced.changes.model_fields_set)

    def test_ambiguous_fields_block_execution_until_clarified(self):
        initial = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(
                kind='meeting', text='Встреча', date_expression='2026-10-29',
            ),
            ambiguous_fields=('time_expression', 'duration'),
        )
        flow = coordinator(Interpreter(
            Interpretation(turn=TurnKind.NEW, intent=initial),
            Interpretation(
                turn=TurnKind.CONTINUE,
                intent=OperationIntent(
                    changes=RecordPatch(time_expression='16:00'),
                ),
            ),
        ))
        waiting = flow.handle(context(), None)
        self.assertEqual(waiting.phase, Phase.NEED_FIELDS)
        self.assertIn('время начала', waiting.question)
        self.assertIn('длительность', waiting.question)

        clarified = flow.handle(
            context(NOW + timedelta(minutes=1)), waiting.pending,
        )
        self.assertEqual(clarified.phase, Phase.NEED_FIELDS)
        self.assertEqual(clarified.pending.intent.ambiguous_fields, ('duration',))

    def test_ambiguous_kind_asks_meeting_or_reminder(self):
        intent = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(text='увидеться с Виталием',
                                date_expression='2026-10-30'),
            ambiguous_fields=('kind',),
            ambiguity_evidence='Хочу увидеться с Виталием завтра',
        )
        result = coordinator(Interpreter(
            Interpretation(turn=TurnKind.NEW, intent=intent),
        )).handle(context(), None)
        self.assertEqual(result.phase, Phase.NEED_FIELDS)
        self.assertEqual(result.question, 'Это встреча или напоминание?')

    def test_selected_ambiguity_role_clears_the_alternative_roles(self):
        previous = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(kind='meeting', text='Встреча'),
            ambiguous_fields=('duration', 'time_expression'),
            ambiguity_values={'duration': '240', 'time_expression': '4'},
            ambiguity_evidence='на 4 часа',
        )
        merged = merge_intent(previous, OperationIntent(
            changes=RecordPatch(duration=DurationInput(minutes=240)),
            resolved_ambiguity='duration',
        ))
        self.assertEqual(merged.ambiguous_fields, ())
        self.assertEqual(merged.ambiguity_values, {})
        self.assertIsNone(merged.ambiguity_evidence)
        self.assertEqual(merged.changes.duration.minutes, 240)

    def test_complete_create_executes_once_without_target_search(self):
        intent = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(kind='task', text='Купить билеты'),
        )
        resolver, executor = Resolver(), Executor()
        result = coordinator(
            Interpreter(Interpretation(turn=TurnKind.NEW, intent=intent)),
            resolver=resolver, executor=executor,
        ).handle(context(), None)
        self.assertEqual(result.phase, Phase.COMPLETED)
        self.assertEqual(len(executor.plans), 1)
        self.assertEqual(resolver.calls, [])

    def test_creation_clarification_keeps_patch_and_operation_identity(self):
        initial = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(kind='meeting', text='Встреча с Марией',
                                date_expression='2026-10-29', time_expression='16:00'),
        )
        first = coordinator(Interpreter(Interpretation(turn=TurnKind.NEW, intent=initial)))
        waiting = first.handle(context(), None)
        self.assertEqual(waiting.phase, Phase.NEED_FIELDS)
        self.assertEqual(waiting.pending.operation_id, OPERATION)
        # duration is absent from intent even though the planned final state lacks it
        self.assertNotIn('duration', waiting.pending.intent.changes.model_fields_set)

        continuation = OperationIntent(
            changes=RecordPatch(duration=DurationInput(minutes=90)),
        )
        executor = Executor()
        second = coordinator(
            Interpreter(Interpretation(turn=TurnKind.CONTINUE, intent=continuation)),
            executor=executor,
        ).handle(context(NOW + timedelta(minutes=1)), waiting.pending)
        self.assertEqual(second.phase, Phase.COMPLETED)
        self.assertEqual(second.records[0].text, 'Встреча с Марией')
        self.assertEqual(second.records[0].duration.minutes, 90)

    def test_conflicting_follow_up_requires_confirmation_and_keeps_safe_date(self):
        initial = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(
                kind='reminder', text='увидеться с Саней',
                time_expression='10:00',
            ),
        )
        patch = OperationIntent(changes=RecordPatch(
            date_expression='2026-10-29', time_expression='11:00',
        ))
        flow = coordinator(Interpreter(
            Interpretation(turn=TurnKind.NEW, intent=initial),
            Interpretation(turn=TurnKind.CONTINUE, intent=patch),
            Interpretation(turn=TurnKind.CONFIRM),
        ))
        waiting = flow.handle(context(), None)
        self.assertEqual(waiting.phase, Phase.NEED_FIELDS)

        conflict = flow.handle(
            context(NOW + timedelta(minutes=1)), waiting.pending,
        )
        self.assertEqual(conflict.phase, Phase.NEED_CONFIRMATION)
        self.assertEqual(
            conflict.pending.intent.changes.date_expression, '2026-10-29',
        )
        self.assertEqual(
            conflict.pending.intent.changes.time_expression, '10:00',
        )
        self.assertEqual(
            conflict.pending.pending_changes[0].field, 'time_expression',
        )

        completed = flow.handle(
            context(NOW + timedelta(minutes=2)), conflict.pending,
        )
        self.assertEqual(completed.phase, Phase.COMPLETED)
        self.assertEqual(completed.records[0].scheduled_date, date(2026, 10, 29))
        self.assertEqual(completed.records[0].scheduled_time, time(11, 0))

    def test_rejected_conflict_keeps_old_value_and_safe_patch(self):
        initial = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(
                kind='reminder', text='увидеться с Саней',
                time_expression='10:00',
            ),
        )
        patch = OperationIntent(changes=RecordPatch(
            date_expression='2026-10-29', time_expression='11:00',
        ))
        flow = coordinator(Interpreter(
            Interpretation(turn=TurnKind.NEW, intent=initial),
            Interpretation(turn=TurnKind.CONTINUE, intent=patch),
            Interpretation(turn=TurnKind.REJECT),
        ))
        waiting = flow.handle(context(), None)
        conflict = flow.handle(
            context(NOW + timedelta(minutes=1)), waiting.pending,
        )
        completed = flow.handle(
            context(NOW + timedelta(minutes=2)), conflict.pending,
        )
        self.assertEqual(completed.phase, Phase.COMPLETED)
        self.assertEqual(completed.records[0].scheduled_date, date(2026, 10, 29))
        self.assertEqual(completed.records[0].scheduled_time, time(10, 0))

    def test_invalid_date_becomes_clarification_and_can_be_corrected(self):
        zoned_now = NOW.astimezone(ZoneInfo('Europe/Moscow'))
        initial = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(
                kind='meeting', text='Встреча с Алексеем',
                date_expression='tomorrow', time_expression='16:00',
                duration=DurationInput(minutes=60),
            ),
        )
        first = coordinator(
            Interpreter(
                Interpretation(turn=TurnKind.NEW, intent=initial),
                Interpretation(
                    turn=TurnKind.CONTINUE,
                    intent=OperationIntent(
                        changes=RecordPatch(date_expression='завтра'),
                    ),
                ),
            ),
            planner=DefaultChangePlanner(),
        )
        waiting = first.handle(context(zoned_now), None)
        self.assertEqual(waiting.phase, Phase.NEED_FIELDS)
        self.assertIn('Уточните дату', waiting.question)
        self.assertNotIn(
            'date_expression', waiting.pending.intent.changes.model_fields_set,
        )

        done = first.handle(
            context(zoned_now + timedelta(minutes=1)), waiting.pending,
        )
        self.assertEqual(done.phase, Phase.COMPLETED)
        self.assertEqual(done.records[0].scheduled_date, date(2026, 10, 29))

    def test_confirmed_create_preserves_allocated_identity(self):
        intent = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(kind='task', text='Купить билеты'),
        )
        executor = Executor()
        flow = coordinator(
            Interpreter(
                Interpretation(turn=TurnKind.NEW, intent=intent),
                Interpretation(turn=TurnKind.CONFIRM),
            ),
            executor=executor,
            policy=DefaultOperationPolicy(
                confirmation_actions=frozenset({Action.CREATE}),
            ),
        )
        proposed = flow.handle(context(), None)
        created_id = proposed.pending.proposed_plan.after.id
        done = flow.handle(context(NOW + timedelta(minutes=1)), proposed.pending)
        self.assertEqual(done.phase, Phase.COMPLETED)
        self.assertEqual(done.records[0].id, created_id)

    def test_update_select_confirm_changes_same_record(self):
        one, two = snapshot(text='Встреча с Марией A'), snapshot(text='Встреча с Марией B')
        intent = OperationIntent(
            action=Action.UPDATE,
            target=TargetQuery(query='Мария', temporal='future'),
            changes=RecordPatch(date_expression='2026-10-30',
                                time_expression='18:00',
                                duration=DurationInput(minutes=120)),
        )
        resolver, planner, executor = Resolver((one, two)), Planner(), Executor()
        flow = coordinator(
            Interpreter(
                Interpretation(turn=TurnKind.NEW, intent=intent),
                Interpretation(turn=TurnKind.CONTINUE, selected_candidate_id=two.id),
                Interpretation(turn=TurnKind.CONFIRM),
            ), resolver, planner, executor,
        )
        choose = flow.handle(context(), None)
        self.assertEqual(choose.phase, Phase.NEED_TARGET)
        proposed = flow.handle(context(NOW + timedelta(minutes=1)), choose.pending)
        self.assertEqual(proposed.phase, Phase.NEED_CONFIRMATION)
        done = flow.handle(context(NOW + timedelta(minutes=2)), proposed.pending)
        self.assertEqual(done.phase, Phase.COMPLETED)
        self.assertEqual(done.records[0].id, two.id)
        self.assertEqual(done.records[0].scheduled_date, date(2026, 10, 30))
        self.assertEqual(done.records[0].duration.minutes, 120)
        self.assertEqual(len(executor.plans), 1)

    def test_model_cannot_select_id_that_was_not_offered(self):
        offered = snapshot()
        intent = OperationIntent(action=Action.CANCEL,
                                 target=TargetQuery(query='Мария'))
        flow = coordinator(
            Interpreter(
                Interpretation(turn=TurnKind.NEW, intent=intent),
                Interpretation(turn=TurnKind.CONTINUE,
                               selected_candidate_id=uuid4()),
            ),
            resolver=Resolver((offered, snapshot(text='Другая встреча'))),
        )
        waiting = flow.handle(context(), None)
        rejected = flow.handle(context(NOW + timedelta(minutes=1)), waiting.pending)
        self.assertEqual(rejected.phase, Phase.REJECTED)
        self.assertEqual(rejected.issues[0].code, 'candidate_not_offered')

    def test_changed_revision_invalidates_confirmation(self):
        current = snapshot(revision='1')
        intent = OperationIntent(
            action=Action.UPDATE,
            target=TargetQuery(query='Мария'),
            changes=RecordPatch(text='Новая встреча'),
        )
        resolver, executor = Resolver((current,)), Executor()
        flow = coordinator(
            Interpreter(
                Interpretation(turn=TurnKind.NEW, intent=intent),
                Interpretation(turn=TurnKind.CONFIRM),
            ), resolver=resolver, executor=executor,
        )
        proposed = flow.handle(context(), None)
        resolver.candidates = (current.model_copy(update={'revision': '2'}),)
        changed = flow.handle(context(NOW + timedelta(minutes=1)), proposed.pending)
        self.assertEqual(changed.phase, Phase.NEED_CONFIRMATION)
        self.assertEqual(changed.issues[0].code, 'plan_changed')
        self.assertEqual(executor.plans, [])

    def test_read_returns_history_without_executor(self):
        old = snapshot(day=date(2025, 1, 1), revision='old')
        future = snapshot(day=date(2027, 1, 1), revision='future')
        executor = Executor()
        result = coordinator(
            Interpreter(Interpretation(
                turn=TurnKind.NEW,
                intent=OperationIntent(action=Action.READ,
                                       target=TargetQuery(query='Мария', temporal='all')),
            )),
            resolver=Resolver((future, old)), executor=executor,
        ).handle(context(), None)
        self.assertEqual(result.phase, Phase.COMPLETED)
        self.assertEqual(result.records, (future, old))
        self.assertEqual(executor.plans, [])

    def test_expired_operation_cannot_be_continued(self):
        intent = OperationIntent(action=Action.CREATE,
                                 changes=RecordPatch(kind='meeting', text='Встреча'))
        pending = PendingOperation(
            operation_id=OPERATION, phase=Phase.NEED_FIELDS, intent=intent,
            created_at=NOW - timedelta(hours=1),
            updated_at=NOW - timedelta(hours=1),
            expires_at=NOW - timedelta(minutes=30),
        )
        result = coordinator(Interpreter(Interpretation(turn=TurnKind.CONTINUE))) \
            .handle(context(), pending)
        self.assertEqual(result.phase, Phase.REJECTED)
        self.assertEqual(result.issues[0].code, 'operation_expired')

    def test_past_cancellation_is_rejected_but_read_is_not(self):
        old = snapshot(day=date(2025, 1, 1))
        intent = OperationIntent(action=Action.CANCEL,
                                 target=TargetQuery(query='Мария', temporal='past'))
        result = coordinator(
            Interpreter(Interpretation(turn=TurnKind.NEW, intent=intent)),
            resolver=Resolver((old,)),
        ).handle(context(), None)
        self.assertEqual(result.phase, Phase.REJECTED)
        self.assertEqual(result.issues[0].code, 'past_cancellation')

    def test_default_planner_changes_multiple_fields_without_new_identity(self):
        current = snapshot(record_id=uuid4())
        intent = OperationIntent(
            action=Action.UPDATE,
            changes=RecordPatch(
                text='Переговоры с Марией',
                date_expression='завтра',
                time_expression='18:30',
                duration=DurationInput(minutes=90),
            ),
        )
        plan = DefaultChangePlanner().prepare(context(), intent, current)
        self.assertEqual(plan.before.id, current.id)
        self.assertEqual(plan.after.id, current.id)
        self.assertEqual(plan.after.text, 'Переговоры с Марией')
        self.assertEqual(plan.after.scheduled_date, date(2026, 10, 29))
        self.assertEqual(plan.after.scheduled_time, time(18, 30))
        self.assertEqual(plan.after.duration.minutes, 90)
        self.assertEqual(plan.after.duration.source, 'user')

    def test_unknown_duration_is_estimated_by_server_policy(self):
        intent = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(
                kind='meeting', text='Встреча с Алексеем',
                date_expression='завтра', time_expression='16:00',
            ),
            unknown_fields=('duration',),
        )
        plan = DefaultChangePlanner().prepare(
            context(NOW.astimezone(ZoneInfo('Europe/Moscow'))), intent, None,
        )
        self.assertEqual(plan.after.duration.minutes, 60)
        self.assertEqual(plan.after.duration.source, 'estimated')
        self.assertIsNone(plan.after.user_duration)
        self.assertEqual(plan.after.duration_status, 'declined_to_specify')
        self.assertEqual(
            plan.after.duration.blocking_source, 'event_type_default',
        )

    def test_default_blocking_does_not_suppress_duration_question(self):
        intent = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(
                kind='meeting', text='Встреча с мамой',
                date_expression='завтра', time_expression='10:00',
            ),
        )
        zoned = context(NOW.astimezone(ZoneInfo('Europe/Moscow')))
        planner = DefaultChangePlanner()
        plan = planner.prepare(zoned, intent, None)
        self.assertIsNone(plan.after.user_duration)
        self.assertEqual(plan.after.duration.minutes, 60)
        self.assertEqual(plan.after.duration_status, 'missing')
        decision = DefaultOperationPolicy().evaluate(zoned, intent, plan, None)
        self.assertEqual(decision.phase, Phase.NEED_FIELDS)
        self.assertEqual(decision.missing_fields[0], 'duration')
        self.assertEqual(decision.question, 'Сколько будет длиться встреча?')

    def test_declined_duration_accepts_default_without_question_loop(self):
        initial = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(
                kind='meeting', text='Встреча с мамой',
                date_expression='завтра', time_expression='10:00',
            ),
        )
        declined = OperationIntent(unknown_fields=('duration',))
        executor = Executor()
        flow = coordinator(
            Interpreter(
                Interpretation(turn=TurnKind.NEW, intent=initial),
                Interpretation(turn=TurnKind.CONTINUE, intent=declined),
            ),
            planner=DefaultChangePlanner(), executor=executor,
        )
        zoned_now = NOW.astimezone(ZoneInfo('Europe/Moscow'))
        waiting = flow.handle(context(zoned_now), None)
        self.assertEqual(waiting.phase, Phase.NEED_FIELDS)
        done = flow.handle(
            context(zoned_now + timedelta(minutes=1)), waiting.pending,
        )
        self.assertEqual(done.phase, Phase.COMPLETED)
        self.assertEqual(done.records[0].duration_status, 'declined_to_specify')
        self.assertEqual(done.records[0].duration.minutes, 60)

    def test_duration_range_blocks_upper_bound_without_losing_range(self):
        intent = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(
                kind='meeting', text='Встреча с мамой',
                date_expression='завтра', time_expression='10:00',
                duration=DurationInput(
                    type='range', min_minutes=60, max_minutes=120,
                    evidence='час-два',
                ),
            ),
        )
        plan = DefaultChangePlanner().prepare(
            context(NOW.astimezone(ZoneInfo('Europe/Moscow'))), intent, None,
        )
        self.assertEqual(plan.after.user_duration.type, 'range')
        self.assertEqual(plan.after.user_duration.min_minutes, 60)
        self.assertEqual(plan.after.user_duration.max_minutes, 120)
        self.assertEqual(plan.after.duration_status, 'provided')
        self.assertEqual(plan.after.duration.minutes, 120)
        self.assertEqual(
            plan.after.duration.blocking_source, 'user_range_max',
        )

    def test_unknown_required_date_gets_explanatory_clarification(self):
        intent = OperationIntent(
            action=Action.CREATE,
            changes=RecordPatch(kind='meeting', text='Встреча с Алексеем'),
            unknown_fields=('date_expression',),
        )
        result = coordinator(
            Interpreter(Interpretation(turn=TurnKind.NEW, intent=intent)),
            planner=DefaultChangePlanner(),
        ).handle(context(NOW.astimezone(ZoneInfo('Europe/Moscow'))), None)
        self.assertEqual(result.phase, Phase.NEED_FIELDS)
        self.assertIn('Без значения поля «дата»', result.question)

    def test_pending_codec_preserves_absent_and_explicit_null_fields(self):
        intent = OperationIntent(
            action=Action.UPDATE,
            changes=RecordPatch(text='Новое название', time_expression=None),
        )
        pending = PendingOperation(
            operation_id=OPERATION, phase=Phase.NEED_FIELDS, intent=intent,
            created_at=NOW, updated_at=NOW, expires_at=NOW + timedelta(minutes=30),
        )
        payload = dump_pending(pending)
        fields = payload['intent']['changes']
        self.assertEqual(fields, {'text': 'Новое название', 'time_expression': None})
        restored = load_pending(payload)
        self.assertEqual(restored.intent.changes.model_fields_set,
                         {'text', 'time_expression'})

    def test_presenter_uses_only_execution_facts(self):
        item = snapshot(text='Встреча с Марией')
        execution = ExecutionResult(action='update', records=(item,))
        from app.domain.operations import OperationOutcome
        outcome = OperationOutcome(
            phase=Phase.COMPLETED, records=(item,), execution=execution,
        )
        answer = DefaultOutcomePresenter().present(context(), outcome)
        self.assertIn('изменена', answer)
        self.assertIn('29.10.2026 в 16:00', answer)
        self.assertNotIn('60', answer)  # estimated/planned duration is not exposed here

    def test_duration_read_distinguishes_plan_from_actual_time(self):
        from app.domain.operations import OperationOutcome
        old = snapshot(
            text='Прошлая встреча', day=date(2025, 1, 1),
            duration=60,
        ).model_copy(update={
            'duration': DurationValue(minutes=60, source='estimated',
                                      description='долго'),
        })
        outcome = OperationOutcome(
            phase=Phase.COMPLETED, records=(old,), read_fields=('duration',),
        )
        answer = DefaultOutcomePresenter().present(context(), outcome)
        self.assertIn('точная длительность не указана', answer)
        self.assertIn('Фактическая продолжительность не зафиксирована', answer)
        self.assertNotIn('60 мин', answer)


if __name__ == '__main__':
    unittest.main()
