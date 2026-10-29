"""Provider-independent state machine for one user operation."""
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
from .state import InvalidTransition, merge_intent, pending_is_expired


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
        interpretation = self.interpreter.interpret(context, pending)
        turn = interpretation.turn

        if turn in (TurnKind.ABANDON, TurnKind.REJECT):
            return OperationOutcome(
                phase=Phase.REJECTED,
                issues=(ValidationIssue(
                    code='operation_abandoned',
                    message='Текущая операция прекращена; сохранённые записи не изменены.',
                ),),
            )

        continuing = turn in (TurnKind.CONTINUE, TurnKind.CONFIRM)
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

        if turn == TurnKind.CONFIRM:
            if pending.phase != Phase.NEED_CONFIRMATION or pending.proposed_plan is None:
                return self._need_intent(
                    'confirmation_not_expected',
                    'Сейчас нет изменения, ожидающего подтверждения.',
                )
            intent = pending.intent
        elif continuing:
            try:
                intent = merge_intent(pending.intent, interpretation.intent)
            except InvalidTransition as error:
                return OperationOutcome(
                    phase=Phase.REJECTED,
                    issues=(ValidationIssue(code=error.code, message=str(error)),),
                )
        else:
            intent = interpretation.intent or OperationIntent()
            pending = None

        if intent.action == Action.UNKNOWN:
            decision = self.policy.evaluate(context, intent, None, pending)
            return self._from_decision(context, intent, decision, pending)

        selected_id = interpretation.selected_candidate_id
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

        if intent.action in (Action.UPDATE, Action.CANCEL):
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
            else:
                return self._need_target(
                    context, intent, pending, candidates, 'target_ambiguous',
                    'Найдено несколько записей. Какую именно выбрать?',
                )

        if intent.action == Action.READ:
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
                              candidate_ids, selected_id, plan)
        return OperationOutcome(phase=decision.phase, pending=state,
                                question=decision.question, issues=decision.issues)

    def _pending(self, context, intent, phase, previous, candidate_ids=(),
                 selected_id=None, plan=None):
        created_at = previous.created_at if previous else context.now
        return PendingOperation(
            operation_id=previous.operation_id if previous else self.operation_id_factory(),
            phase=phase,
            intent=intent,
            candidate_ids=tuple(candidate_ids),
            selected_record_id=selected_id,
            proposed_plan=plan,
            created_at=created_at,
            updated_at=context.now,
            expires_at=context.now + self.pending_ttl,
        )

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
