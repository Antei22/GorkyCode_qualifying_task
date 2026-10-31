"""Provider-independent invariants for complete operation plans."""
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.domain.operations import (
    Action, ChangePlan, OperationIntent, PendingOperation, Phase, PolicyDecision,
    ValidationIssue, plan_fingerprint,
)
from app.domain.time import DateRange, PeriodExpression
from .ports import TurnContext
from .confirmation import build_update_confirmation


_QUESTIONS = {
    'kind': 'Что создать: задачу, встречу или напоминание?',
    'text': 'Что должно быть записано?',
    'scheduled_date': 'На какую дату?',
    'scheduled_time': 'На какое время?',
    'duration': 'Сколько будет длиться встреча?',
    'timezone': 'Уточните часовой пояс.',
}


def _missing_question(field, kind_candidates=(), text='', explicitly_unknown=False):
    if explicitly_unknown:
        labels = {
            'kind': 'тип записи', 'text': 'содержание записи',
            'scheduled_date': 'дата', 'scheduled_time': 'время',
            'timezone': 'часовой пояс', 'duration': 'длительность',
        }
        return f'Без значения поля «{labels[field]}» операцию завершить нельзя. Уточните его.'
    if field == 'kind':
        labels = {'task': 'задача', 'meeting': 'встреча',
                  'reminder': 'напоминание'}
        candidates = tuple(kind_candidates)
        if len(candidates) == 2 and all(item in labels for item in candidates):
            return f'Это {labels[candidates[0]]} или {labels[candidates[1]]}?'
    return _QUESTIONS[field]


@dataclass(frozen=True)
class DefaultOperationPolicy:
    """Validate server plans; storage adapters may add conflict checks.

    Updates and cancellations require confirmation by default. A deployment can
    also require it for creation without changing the coordinator.
    """
    confirmation_actions: frozenset[Action] = frozenset({Action.UPDATE, Action.CANCEL})

    def evaluate(self, context: TurnContext, intent: OperationIntent,
                 plan: ChangePlan | None,
                 pending: PendingOperation | None) -> PolicyDecision:
        if intent.action == Action.UNKNOWN:
            return PolicyDecision(
                phase=Phase.NEED_INTENT,
                question='Что нужно сделать: создать, изменить, отменить или посмотреть запись?',
            )
        if intent.ambiguous_fields:
            fields = set(intent.ambiguous_fields)
            evidence = (intent.ambiguity_evidence or '').strip()
            if fields == {'kind'}:
                labels = {
                    'task': 'задача', 'meeting': 'встреча',
                    'reminder': 'напоминание',
                }
                candidates = intent.kind_candidates or ('meeting', 'reminder')
                if len(candidates) == 2:
                    question = (
                        f'Это {labels[candidates[0]]} или '
                        f'{labels[candidates[1]]}?'
                    )
                else:
                    question = _QUESTIONS['kind']
            elif (intent.changes.kind == 'reminder'
                  and 'time_expression' in fields and evidence):
                question = (f'«{evidence}» — это время напоминания '
                            'или часть текста?')
            elif fields == {'time_expression', 'duration'}:
                cited = f'«{evidence}»' if evidence else 'указанный фрагмент'
                question = (f'Что вы имеете в виду под {cited}: '
                            'длительность встречи или время начала?')
            else:
                labels = {
                    'kind': 'тип записи', 'text': 'текст',
                    'date_expression': 'дату', 'time_expression': 'время начала',
                    'timezone': 'часовой пояс', 'duration': 'длительность',
                }
                names = ', '.join(labels[item] for item in intent.ambiguous_fields)
                cited = f' во фрагменте «{evidence}»' if evidence else ''
                question = f'Уточните неоднозначные параметры{cited}: {names}.'
            context.trace.update({
                'ambiguous_span': evidence or None,
                'candidate_roles': list(intent.ambiguous_fields),
                'clarification_text': question,
            })
            return PolicyDecision(
                phase=Phase.NEED_FIELDS,
                missing_fields=tuple(intent.ambiguous_fields),
                question=question,
            )
        supplied_changes = intent.changes.model_fields_set
        if intent.action == Action.READ and (supplied_changes or intent.unknown_fields):
            return self._reject('read_with_changes', 'Просмотр не должен изменять запись.')
        if intent.action == Action.READ:
            return PolicyDecision(phase=Phase.READY)
        if (intent.action == Action.UPDATE and not supplied_changes
                and not intent.unknown_fields):
            return self._reject('changes_missing', 'Не указано, что изменить.')
        if intent.action == Action.CANCEL and (supplied_changes or intent.unknown_fields):
            return self._reject('cancel_with_changes', 'Отмена записи не должна менять её поля.')
        if plan is None:
            return PolicyDecision(
                phase=Phase.NEED_TARGET if intent.action != Action.CREATE else Phase.NEED_FIELDS,
                issues=(ValidationIssue(code='plan_missing', message='Не удалось подготовить изменение.'),),
            )
        expected_action = intent.action.value
        if plan.action != expected_action:
            return self._reject('action_mismatch', 'Подготовлен план другого действия.')
        if plan.fingerprint != plan_fingerprint(plan.action, plan.before, plan.after):
            return self._reject('invalid_fingerprint', 'План изменения повреждён.')
        if plan.before is not None and plan.before.status != 'open':
            return self._reject('target_not_open', 'Эту запись уже нельзя изменить.')

        final = plan.after
        missing = []
        if final.kind is None:
            missing.append('kind')
        if not final.text.strip():
            missing.append('text')
        if not final.timezone.strip():
            missing.append('timezone')
        if final.kind in ('meeting', 'reminder'):
            if final.scheduled_date is None:
                missing.append('scheduled_date')
            if final.scheduled_time is None:
                missing.append('scheduled_time')
        if (final.kind == 'meeting'
                and (final.duration_status == 'missing'
                     or final.duration.minutes is None)):
            missing.append('duration')
        if final.scheduled_time is not None and final.scheduled_date is None:
            missing.append('scheduled_date')
        if missing:
            unique = tuple(dict.fromkeys(missing))
            source_field = {
                'scheduled_date': 'date_expression',
                'scheduled_time': 'time_expression',
            }.get(unique[0], unique[0])
            question = _missing_question(
                unique[0], intent.kind_candidates, text=final.text,
                explicitly_unknown=source_field in intent.unknown_fields,
            )
            if unique[0] == 'scheduled_date' and intent.changes.temporal is not None:
                temporal = intent.changes.temporal
                subject = 'встречу' if final.kind == 'meeting' else 'напоминание'
                if isinstance(temporal, PeriodExpression) and temporal.unit == 'week':
                    label = 'следующей неделе' if temporal.offset == 1 else 'этой неделе'
                    question = f'В какой день на {label} назначить {subject}?'
                elif isinstance(temporal, DateRange):
                    question = f'В какой день из указанного диапазона назначить {subject}?'
            return PolicyDecision(
                phase=Phase.NEED_FIELDS,
                missing_fields=unique,
                question=question,
            )

        try:
            ZoneInfo(final.timezone)
        except ZoneInfoNotFoundError:
            return self._reject('invalid_timezone', 'Неизвестный часовой пояс.')

        scheduled = self._scheduled_at(context.now, final.scheduled_date,
                                       final.scheduled_time, final.timezone)
        if intent.action in (Action.CREATE, Action.UPDATE) and scheduled is not None:
            if scheduled < context.now:
                return self._reject('past_schedule', 'Нельзя назначить запись в прошлом.')
        if intent.action == Action.CANCEL:
            before = plan.before
            previous = self._scheduled_at(context.now, before.scheduled_date,
                                          before.scheduled_time,
                                          before.timezone) if before else None
            if previous is not None and previous < context.now:
                return self._reject('past_cancellation', 'Прошедшую запись нельзя отменить.')

        if intent.action in self.confirmation_actions:
            confirmed = pending is not None and pending.confirmed_fingerprint == plan.fingerprint
            if not confirmed:
                if intent.action == Action.UPDATE:
                    question = build_update_confirmation(plan.before, plan.after)
                else:
                    before = plan.before
                    when = ''
                    if before and before.scheduled_date:
                        when = f' — {before.scheduled_date:%d.%m.%Y}'
                        if before.scheduled_time:
                            when += f' в {before.scheduled_time:%H:%M}'
                    question = (f'Подтвердите удаление события '
                                f'«{before.text if before else ""}»{when}?')
                return PolicyDecision(
                    phase=Phase.NEED_CONFIRMATION,
                    question=question,
                )
        return PolicyDecision(phase=Phase.READY)

    @staticmethod
    def _scheduled_at(now: datetime, day, clock: time | None, timezone: str):
        if day is None:
            return None
        value = datetime.combine(day, clock or time(23, 59, 59))
        return value.replace(tzinfo=ZoneInfo(timezone))

    @staticmethod
    def _reject(code: str, message: str) -> PolicyDecision:
        return PolicyDecision(
            phase=Phase.REJECTED,
            issues=(ValidationIssue(code=code, message=message),),
        )
