"""Provider-independent state machine for one user operation."""
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID, uuid4

from app.domain.models import FieldInputError

from app.domain.operations import (
    Action, OperationIntent, OperationOutcome, PendingOperation, Phase,
    PolicyDecision, RecordPatch, TargetQuery, TurnKind, ValidationIssue,
)
from .ports import (
    ChangePlanner, IntentInterpreter, OperationExecutor, OperationPolicy,
    TargetResolver, TurnContext,
)
from .confirmation import ConfirmationResolver, build_pending_change_confirmation
from .state import (
    InvalidTransition, apply_pending_changes, merge_intent, merge_patch,
    pending_is_expired,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OperationCoordinator:
    interpreter: IntentInterpreter
    resolver: TargetResolver
    planner: ChangePlanner
    policy: OperationPolicy
    executor: OperationExecutor
    pending_ttl: timedelta = timedelta(minutes=30)
    operation_id_factory: Callable[[], UUID] = uuid4

    def handle(self, context: TurnContext,
               pending: PendingOperation | None) -> OperationOutcome:
        """Advance the state once; persistence remains the caller's transaction.

        The method invokes at most one executor and never catches an executor error:
        Service can therefore roll back the complete request and replay request_id.
        """
        if pending is not None and pending.phase == Phase.NEED_CONFIRMATION:
            turn = ConfirmationResolver.resolve(context.message)
            interpretation = None
            if turn == TurnKind.UNKNOWN:
                interpretation = self.interpreter.interpret(context, pending)
                turn = interpretation.turn
            context.trace.update({
                'parser_used': ('confirmation_resolver' if interpretation is None
                                else interpretation.parser_used),
                'confirmation_decision': {
                    TurnKind.CONFIRM: 'confirm', TurnKind.REJECT: 'reject',
                    TurnKind.CORRECTION: 'correction',
                }.get(turn, 'unknown'),
                'decision': {
                    TurnKind.CONFIRM: 'confirm', TurnKind.REJECT: 'reject',
                    TurnKind.CORRECTION: 'correction',
                }.get(turn, 'unknown'),
                'correction_patch': (
                    interpretation.intent.changes.model_dump(
                        mode='json', exclude_unset=True,
                    ) if interpretation is not None and interpretation.intent else None
                ),
                'llm_output': (interpretation.llm_output
                               if interpretation is not None else None),
            })
        else:
            interpretation = self.interpreter.interpret(context, pending)
            turn = interpretation.turn

        if (pending is not None and pending.phase == Phase.NEED_CONFIRMATION
                and turn == TurnKind.UNKNOWN):
            return OperationOutcome(
                phase=Phase.NEED_CONFIRMATION,
                pending=pending.model_copy(update={
                    'updated_at': context.now,
                    'expires_at': context.now + self.pending_ttl,
                }),
                question=('Ответьте «да» для подтверждения, «нет» для отказа '
                          'или укажите исправленные данные.'),
            )

        resolved_conflict_intent = None
        if (pending is not None and pending.pending_changes
                and turn in (TurnKind.CONFIRM, TurnKind.REJECT)):
            resolved_conflict_intent = (
                apply_pending_changes(pending.intent, pending.pending_changes)
                if turn == TurnKind.CONFIRM else pending.intent
            )
            pending = pending.model_copy(update={
                'phase': Phase.NEED_FIELDS,
                'pending_changes': (),
                'updated_at': context.now,
                'expires_at': context.now + self.pending_ttl,
            })
            turn = TurnKind.CONTINUE

        if turn in (TurnKind.ABANDON, TurnKind.REJECT):
            return OperationOutcome(
                phase=Phase.REJECTED,
                issues=(ValidationIssue(
                    code='operation_abandoned',
                    message='Текущая операция прекращена; сохранённые записи не изменены.',
                ),),
            )

        continuing = turn in (
            TurnKind.CONTINUE, TurnKind.CONFIRM, TurnKind.CORRECTION,
        )
        if pending is not None and pending_is_expired(pending, context.now):
            if turn != TurnKind.NEW:
                return OperationOutcome(
                    phase=Phase.REJECTED,
                    issues=(ValidationIssue(
                        code='operation_expired',
                        message='Черновик устарел. Повторите команду заново.',
                    ),),
                )
            pending = None
        if continuing and pending is None:
            return self._need_intent('pending_missing', 'Нет операции, которую можно продолжить.')

        if resolved_conflict_intent is not None:
            intent = resolved_conflict_intent
        elif turn == TurnKind.CONFIRM:
            if pending.phase != Phase.NEED_CONFIRMATION or pending.proposed_plan is None:
                return self._need_intent(
                    'confirmation_not_expected',
                    'Сейчас нет изменения, ожидающего подтверждения.',
                )
            intent = pending.intent
        elif continuing:
            try:
                intent, accepted, same, conflicts = merge_patch(
                    pending.intent, interpretation.intent,
                    allow_overwrite=turn == TurnKind.CORRECTION,
                )
            except InvalidTransition as error:
                return OperationOutcome(
                    phase=Phase.REJECTED,
                    issues=(ValidationIssue(code=error.code, message=str(error)),),
                )
            old_values = {item.field: item.old_value for item in conflicts}
            new_values = intent.changes.model_dump(
                mode='json', exclude_unset=True,
            )
            new_values.update({item.field: item.new_value for item in conflicts})
            confirmation_message = (
                build_pending_change_confirmation(
                    intent.changes.kind, old_values, new_values,
                ) if conflicts else None
            )
            logger.info('patch_merge %s', json.dumps({
                'raw_text': context.message,
                'current_draft': pending.intent.changes.model_dump(
                    mode='json', exclude_unset=True,
                ),
                'expected_field': self._expected_field(pending),
                'llm_patch': (interpretation.intent.changes.model_dump(
                    mode='json', exclude_unset=True,
                ) if interpretation.intent else {}),
                'merge_result': {
                    'accepted': {
                        field: intent.changes.model_dump(
                            mode='json', exclude_unset=True,
                        ).get(field)
                        for field in accepted
                    },
                    'conflicts': {
                        item.field: {
                            'old': item.old_value, 'new': item.new_value,
                        } for item in conflicts
                    },
                },
                'accepted_fields': accepted,
                'same_fields': same,
                'conflicting_fields': [item.field for item in conflicts],
                'pending_changes': [item.model_dump(mode='json') for item in conflicts],
                'old_values': old_values,
                'new_values': new_values,
                'confirmation_message': confirmation_message,
            }, ensure_ascii=False))
            if conflicts:
                state = self._pending(
                    context, intent, Phase.NEED_CONFIRMATION, pending,
                    plan=pending.proposed_plan,
                    pending_changes=conflicts,
                )
                return OperationOutcome(
                    phase=Phase.NEED_CONFIRMATION,
                    pending=state,
                    question=confirmation_message,
                )
        else:
            intent = interpretation.intent or OperationIntent()
            pending = None

<<<<<<< Updated upstream
=======
        if (turn == TurnKind.CONFIRM and pending is not None
                and pending.proposed_batch is not None):
            batch = pending.proposed_batch
            if batch.fingerprint != batch_fingerprint(batch.action, batch.plans):
                return OperationOutcome(
                    phase=Phase.REJECTED,
                    issues=(ValidationIssue(code='invalid_batch_fingerprint', message='Пакет изменений повреждён.'),),
                )
            records = []
            for saved_plan in batch.plans:
                execution = self.executor.execute(context, saved_plan)
                if execution.action != saved_plan.action or len(execution.records) != 1:
                    raise RuntimeError('operation executor returned a different batch action')
                records.extend(execution.records)
            executed = tuple(records)
            logger.info('batch_confirmed action=%s ids=%s', batch.action,
                        [str(item.id) for item in executed])
            context.trace['confirmed_ids'] = [str(item.id) for item in executed]
            return OperationOutcome(
                phase=Phase.COMPLETED, records=executed,
                execution=ExecutionResult(action=batch.action, records=executed),
            )

>>>>>>> Stashed changes
        if intent.action == Action.UNKNOWN:
            decision = self.policy.evaluate(context, intent, None, pending)
            return self._from_decision(context, intent, decision, pending)

        if intent.action == Action.LIST:
            resolution = self.resolver.list(context, intent)
            context.trace['candidate_ids'] = [
                str(item.id) for item in resolution.candidates
            ]
            return OperationOutcome(
                phase=Phase.COMPLETED,
                records=resolution.candidates,
                read_fields=intent.read_fields,
            )

        selected_id = (interpretation.selected_candidate_id
                       if interpretation is not None else None)
        if selected_id is not None and not continuing:
            return OperationOutcome(
                phase=Phase.REJECTED,
                issues=(ValidationIssue(
                    code='candidate_not_expected',
                    message='Выбор записи допустим только из ранее показанного списка.',
                ),),
            )
        retargeted = (
            turn == TurnKind.CONTINUE
            and interpretation is not None
            and interpretation.intent is not None
            and bool(interpretation.intent.target.model_fields_set)
        )
        if selected_id is None and continuing and pending is not None and not retargeted:
            selected_id = pending.selected_record_id
        if selected_id is not None and continuing and pending is not None:
            allowed = set(pending.candidate_ids)
            if pending.selected_record_id is not None:
                allowed.add(pending.selected_record_id)
            if selected_id not in allowed:
                return OperationOutcome(
                    phase=Phase.REJECTED,
                    issues=(ValidationIssue(
                        code='candidate_not_offered',
                        message='Выбранная запись не относится к текущему списку.',
                    ),),
                )

        target = None
        resolution = None
        if intent.action != Action.CREATE:
            try:
                resolution = self.resolver.resolve(
                    context, intent, selected_id=selected_id,
                )
            except FieldInputError as error:
                intent = self._without_invalid_field(intent, error.field, target=True)
                decision = PolicyDecision(
                    phase=Phase.NEED_TARGET,
                    question=str(error),
                    issues=(ValidationIssue(
                        code='invalid_target_field', field=error.field,
                        message=str(error),
                    ),),
                )
                return self._from_decision(context, intent, decision, pending)

<<<<<<< Updated upstream
        if intent.action in (Action.UPDATE, Action.CANCEL):
=======
        if (intent.action in (Action.UPDATE, Action.CANCEL, Action.COMPLETE, Action.REOPEN)
                and not (intent.action in (Action.CANCEL, Action.COMPLETE, Action.REOPEN)
                         and intent.all_matches)):
>>>>>>> Stashed changes
            candidates = resolution.candidates
            if selected_id is not None:
                target = next((item for item in candidates if item.id == selected_id), None)
                if target is None:
                    return self._need_target(
                        context, intent, pending, candidates,
                        'selected_target_unavailable',
                        'Выбранная запись больше недоступна. Уточните запись заново.',
                    )
            elif len(candidates) == 1:
                target = candidates[0]
                selected_id = target.id
            elif not candidates:
                return self._need_target(
                    context, intent, pending, (), 'target_not_found',
                    'Подходящая запись не найдена. Уточните название или дату.',
                )
<<<<<<< Updated upstream
            if selected_id is not None:
                context.trace['selected_id'] = str(selected_id)
=======
>>>>>>> Stashed changes
            else:
                return self._need_target(
                    context, intent, pending, candidates, 'target_ambiguous',
                    'Найдено несколько записей. Какую именно выбрать?',
                )
<<<<<<< Updated upstream
=======

        if intent.action in (Action.CANCEL, Action.COMPLETE, Action.REOPEN) and intent.all_matches:
            candidates = resolution.candidates
            if not candidates:
                return self._need_target(context, intent, pending, (), 'target_not_found',
                                         'Подходящих записей не найдено. Уточните название или дату.')
            plans = tuple(self.planner.prepare(context, intent, item) for item in candidates)
            batch = BatchChangePlan(
                action=intent.action.value,
                plans=plans,
                fingerprint=batch_fingerprint(intent.action.value, plans),
            )
            state = self._pending(
                context, intent, Phase.NEED_CONFIRMATION, pending,
                candidate_ids=tuple(item.id for item in candidates),
                plan=None, proposed_batch=batch,
            )
            if intent.action == Action.CANCEL:
                question = f'Подтвердите удаление этих {len(plans)} событий?'
            else:
                verb = ('выполненными' if intent.action == Action.COMPLETE
                        else 'в активные')
                question = f'Отметить {verb} все {len(plans)}?'
            frozen_ids = [str(item.id) for item in candidates]
            context.trace.update({
                'bulk_action': intent.action.value,
                'matched_ids': frozen_ids,
                'frozen_ids': frozen_ids,
            })
            logger.info('batch_preview action=%s matched_ids=%s frozen_ids=%s',
                        intent.action.value, frozen_ids, frozen_ids)
            return OperationOutcome(
                phase=Phase.NEED_CONFIRMATION, pending=state,
                question=question, records=candidates,
            )
>>>>>>> Stashed changes

        if intent.action == Action.READ:
            if (intent.read_fields and selected_id is None
                    and len(resolution.candidates) > 1):
                return self._need_target(
                    context, intent, pending, resolution.candidates,
                    'target_ambiguous',
                    'Найдено несколько подходящих записей. Какую именно выбрать?',
                )
            decision = self.policy.evaluate(context, intent, None, pending)
            if decision.phase == Phase.READY:
                return OperationOutcome(phase=Phase.COMPLETED,
                                        records=resolution.candidates,
                                        read_fields=intent.read_fields)
            return self._from_decision(
                context, intent, decision, pending,
                candidate_ids=tuple(item.id for item in resolution.candidates),
            )

        previous_plan = pending.proposed_plan if continuing and pending is not None else None
        try:
            plan = self.planner.prepare(
                context, intent, target, previous_plan=previous_plan,
            )
        except FieldInputError as error:
            intent = self._without_invalid_field(intent, error.field, target=False)
            missing = {'date_expression': 'scheduled_date',
                       'time_expression': 'scheduled_time'}.get(error.field, error.field)
            decision = PolicyDecision(
                phase=Phase.NEED_FIELDS,
                missing_fields=(missing,),
                question=str(error),
                issues=(ValidationIssue(
                    code='invalid_change_field', field=error.field,
                    message=str(error),
                ),),
            )
            return self._from_decision(
                context, intent, decision, pending,
                candidate_ids=tuple(item.id for item in resolution.candidates)
                if resolution else (),
                selected_id=selected_id,
            )
        policy_pending = pending
        plan_changed = False
        if turn == TurnKind.CONFIRM:
            plan_changed = pending.proposed_plan.fingerprint != plan.fingerprint
            if not plan_changed:
                policy_pending = pending.model_copy(update={
                    'confirmed_fingerprint': plan.fingerprint,
                    'updated_at': context.now,
                    'expires_at': context.now + self.pending_ttl,
                })

        decision = self.policy.evaluate(context, intent, plan, policy_pending)
        if plan_changed and decision.phase in (Phase.READY, Phase.NEED_CONFIRMATION):
            issue = ValidationIssue(
                code='plan_changed',
                message='Запись изменилась после предложения. Проверьте новый вариант.',
            )
            decision = decision.model_copy(update={
                'phase': Phase.NEED_CONFIRMATION,
                'question': decision.question or 'План изменился. Подтвердить новый вариант?',
                'issues': decision.issues + (issue,),
            })

        if decision.alternative_plan is not None:
            plan = decision.alternative_plan
            intent = decision.alternative_intent

        if decision.phase != Phase.READY:
            return self._from_decision(
                context, intent, decision, pending,
                candidate_ids=tuple(item.id for item in resolution.candidates) if resolution else (),
                selected_id=selected_id, plan=plan,
            )

        execution = self.executor.execute(context, plan)
        if execution.action != plan.action:
            raise RuntimeError('operation executor returned a different action')
        if len(execution.records) != 1 or execution.records[0].id != plan.after.id:
            raise RuntimeError('operation executor returned a different record')
        actual = execution.records[0].model_dump(exclude={'revision'})
        expected = plan.after.model_dump(exclude={'revision'})
        if actual != expected:
            raise RuntimeError('operation executor result differs from the approved plan')
        return OperationOutcome(
            phase=Phase.COMPLETED,
            records=execution.records,
            execution=execution,
        )

    def _from_decision(self, context, intent, decision, previous,
                       candidate_ids=(), selected_id=None, plan=None):
        if decision.phase in (Phase.COMPLETED, Phase.REJECTED):
            return OperationOutcome(phase=decision.phase, question=decision.question,
                                    issues=decision.issues)
        state = self._pending(context, intent, decision.phase, previous,
                              candidate_ids, selected_id, plan,
                              calendar_clarification=decision.calendar_clarification)
        return OperationOutcome(phase=decision.phase, pending=state,
                                question=decision.question, issues=decision.issues)

    def _pending(self, context, intent, phase, previous, candidate_ids=(),
<<<<<<< Updated upstream
                 selected_id=None, plan=None, pending_changes=()):
=======
                 selected_id=None, plan=None, pending_changes=(), proposed_batch=None,
                 calendar_clarification=None):
>>>>>>> Stashed changes
        created_at = previous.created_at if previous else context.now
        return PendingOperation(
            operation_id=previous.operation_id if previous else self.operation_id_factory(),
            phase=phase,
            intent=intent,
            candidate_ids=tuple(candidate_ids),
            selected_record_id=selected_id,
            proposed_plan=plan,
            pending_changes=tuple(pending_changes),
            calendar_clarification=calendar_clarification,
            created_at=created_at,
            updated_at=context.now,
            expires_at=context.now + self.pending_ttl,
        )

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
            day, clock = changes.date_expression, changes.time_expression
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

    @staticmethod
    def _display_value(value):
        if isinstance(value, dict):
            if value.get('type') in {'exact', 'approximate'}:
                return f'{value.get("minutes")} мин.'
            if value.get('type') == 'range':
                return f'{value.get("min_minutes")}–{value.get("max_minutes")} мин.'
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def _need_target(self, context, intent, previous, candidates, code, message):
        decision = PolicyDecision(
            phase=Phase.NEED_TARGET,
            question=message,
            issues=(ValidationIssue(code=code, message=message),),
        )
        outcome = self._from_decision(
            context, intent, decision, previous,
            candidate_ids=tuple(item.id for item in candidates),
        )
        return outcome.model_copy(update={'records': tuple(candidates)})

    @staticmethod
    def _without_invalid_field(intent, field, target):
        current = intent.target if target else intent.changes
        values = current.model_dump(exclude_unset=True)
        values.pop(field, None)
        replacement = (TargetQuery.model_validate(values) if target
                       else RecordPatch.model_validate(values))
        return intent.model_copy(update={
            'target' if target else 'changes': replacement,
        })

    @staticmethod
    def _need_intent(code, message):
        return OperationOutcome(
            phase=Phase.NEED_INTENT,
            question=message,
            issues=(ValidationIssue(code=code, message=message),),
        )
