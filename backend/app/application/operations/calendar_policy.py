"""Calendar conflict policy layered over the generic operation invariants."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from app.domain.operations import (
    Action, OperationIntent, Phase, RecordPatch, make_change_plan,
)
from .policy import DefaultOperationPolicy
from .state import merge_intent


@dataclass(frozen=True)
class CalendarOperationPolicy:
    repository: object
    connection: object
    base: DefaultOperationPolicy = DefaultOperationPolicy()

    def evaluate(self, context, intent, plan, pending):
        decision = self.base.evaluate(context, intent, plan, pending)
        if decision.phase in (Phase.REJECTED, Phase.NEED_FIELDS, Phase.NEED_INTENT,
                              Phase.NEED_TARGET) or plan is None:
            return decision
        if intent.action not in (Action.CREATE, Action.UPDATE):
            return decision
        after = plan.after
        if (after.kind != 'meeting' or after.scheduled_date is None
                or after.scheduled_time is None or after.duration.minutes is None):
            return decision
        if intent.alternative_requested:
            return self._alternative(context, intent, plan, decision)
        conflict, nearest = self._nearest(
            context.owner_id,
            datetime.combine(after.scheduled_date, after.scheduled_time),
            after.duration.minutes,
            after.id if intent.action == Action.UPDATE else None,
        )
        if conflict is None:
            return decision
        if nearest is None:
            return decision.model_copy(update={
                'phase': Phase.NEED_FIELDS,
                'missing_fields': ('scheduled_date', 'scheduled_time'),
                'question': ('В ближайшие 30 дней нет подходящего свободного времени '
                             'в рабочие часы. Укажите другую дату и время.'),
            })
        replacement = OperationIntent(
            action=Action.UNKNOWN,
            changes=RecordPatch(
                date_expression=nearest.date().isoformat(),
                time_expression=nearest.strftime('%H:%M'),
            ),
        )
        alternative_intent = merge_intent(intent, replacement)
        alternative_after = after.model_copy(update={
            'scheduled_date': nearest.date(),
            'scheduled_time': nearest.time(),
        })
        alternative_plan = make_change_plan(
            plan.action, plan.before, alternative_after,
        )
        question = (
            f'На {after.scheduled_date:%d.%m.%Y} в {after.scheduled_time:%H:%M} '
            f'уже запланирована встреча «{conflict["text"]}». '
            f'Ближайшее свободное время — {nearest:%d.%m.%Y в %H:%M}. Подтвердить?'
        )
        return decision.model_copy(update={
            'phase': Phase.NEED_CONFIRMATION,
            'question': question,
            'alternative_plan': alternative_plan,
            'alternative_intent': alternative_intent,
        })

    def _alternative(self, context, intent, plan, decision):
        after = plan.after
        current = datetime.combine(after.scheduled_date, after.scheduled_time)
        nearest = self._next_free(
            context.owner_id, current, after.duration.minutes,
            after.id if intent.action == Action.UPDATE else None,
        )
        if nearest is None:
            return decision.model_copy(update={
                'phase': Phase.NEED_FIELDS,
                'missing_fields': ('scheduled_date', 'scheduled_time'),
                'question': ('Другого подходящего времени в рабочие часы на '
                             'ближайшие 30 дней нет. Укажите желаемую дату и время.'),
            })
        replacement = OperationIntent(
            action=Action.UNKNOWN,
            changes=RecordPatch(
                date_expression=nearest.date().isoformat(),
                time_expression=nearest.strftime('%H:%M'),
            ),
        )
        alternative_intent = merge_intent(intent, replacement).model_copy(
            update={'alternative_requested': False},
        )
        alternative_after = after.model_copy(update={
            'scheduled_date': nearest.date(),
            'scheduled_time': nearest.time(),
        })
        alternative_plan = make_change_plan(
            plan.action, plan.before, alternative_after,
        )
        return decision.model_copy(update={
            'phase': Phase.NEED_CONFIRMATION,
            'question': (
                f'Следующее свободное время — {nearest:%d.%m.%Y в %H:%M}. '
                'Подтвердить?'
            ),
            'alternative_plan': alternative_plan,
            'alternative_intent': alternative_intent,
        })

    def _nearest(self, owner, requested, duration, exclude_id):
        first, last = requested.date(), requested.date() + timedelta(days=30)
        rows = self.repository.meetings_between(self.connection, owner, first, last)
        by_day = {}
        for row in rows:
            if row['id'] == exclude_id:
                continue
            start = datetime.combine(row['scheduled_date'], row['scheduled_time'])
            end = start + timedelta(minutes=row.get('duration_minutes') or 60)
            by_day.setdefault(start.date(), []).append((start, end, row))
        requested_end = requested + timedelta(minutes=duration)
        conflicts = [item for item in by_day.get(requested.date(), ())
                     if item[0] < requested_end and item[1] > requested]
        if not conflicts:
            return None, None
        for offset in range(31):
            day = first + timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            candidate = max(requested, datetime.combine(day, time(9)))
            closing = datetime.combine(day, time(18))
            while candidate + timedelta(minutes=duration) <= closing:
                end = candidate + timedelta(minutes=duration)
                overlaps = [item for item in by_day.get(day, ())
                            if item[0] < end and item[1] > candidate]
                if not overlaps:
                    return conflicts[0][2], candidate
                candidate = max(item[1] for item in overlaps)
        return conflicts[0][2], None

    def _next_free(self, owner, current, duration, exclude_id):
        first, last = current.date(), current.date() + timedelta(days=30)
        rows = self.repository.meetings_between(
            self.connection, owner, first, last,
        )
        by_day = {}
        for row in rows:
            if row['id'] == exclude_id:
                continue
            start = datetime.combine(row['scheduled_date'], row['scheduled_time'])
            end = start + timedelta(minutes=row.get('duration_minutes') or 60)
            by_day.setdefault(start.date(), []).append((start, end))
        earliest = current + timedelta(minutes=30)
        for offset in range(31):
            day = first + timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            opening = datetime.combine(day, time(9))
            candidate = max(earliest, opening) if day == first else opening
            closing = datetime.combine(day, time(18))
            while candidate + timedelta(minutes=duration) <= closing:
                end = candidate + timedelta(minutes=duration)
                overlaps = [item for item in by_day.get(day, ())
                            if item[0] < end and item[1] > candidate]
                if not overlaps:
                    return candidate
                candidate = max(item[1] for item in overlaps)
        return None
