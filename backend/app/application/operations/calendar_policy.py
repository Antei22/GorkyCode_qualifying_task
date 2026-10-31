"""Calendar conflict policy layered over the generic operation invariants."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from app.domain.operations import (
    Action, OperationIntent, Phase, RecordPatch, make_change_plan,
)
from .policy import DefaultOperationPolicy
from .state import merge_intent


WORKING_DAY_START = time(8)
WORKING_DAY_END = time(18)


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
        requested = datetime.combine(after.scheduled_date, after.scheduled_time)
        conflict, nearest, occupied, available_after = self._nearest(
            context.owner_id,
            requested,
            after.duration.minutes,
            after.id if intent.action == Action.UPDATE else None,
        )
        context.trace.update({
            'requested_start': datetime.combine(
                after.scheduled_date, after.scheduled_time,
            ).isoformat(),
            'requested_duration_minutes': after.duration.minutes,
            'requested_duration': after.duration.minutes,
            'occupied_intervals': occupied,
            'nearest_free_slot': nearest.isoformat() if nearest else None,
            'available_after': available_after.isoformat(),
        })
        opening = datetime.combine(after.scheduled_date, WORKING_DAY_START)
        closing = datetime.combine(after.scheduled_date, WORKING_DAY_END)
        fits_working_window = (
            requested >= opening
            and requested + timedelta(minutes=after.duration.minutes) <= closing
        )
        if not fits_working_window and not intent.working_hours_override:
            return self._working_window_clarification(
                decision, available_after,
            )
        if conflict is None:
            return decision
        if nearest is None:
            return self._working_window_clarification(
                decision, available_after,
            )
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
            f'уже запланирована встреча «{conflict["text"]}»; для неё '
            f'зарезервировано время до {self._reserved_until(conflict):%H:%M}. '
            f'Ближайшее свободное время — {nearest:%d.%m.%Y в %H:%M}. Подтвердить?'
        )
        return decision.model_copy(update={
            'phase': Phase.NEED_CONFIRMATION,
            'question': question,
            'alternative_plan': alternative_plan,
            'alternative_intent': alternative_intent,
        })

    @staticmethod
    def _reserved_until(row):
        start = datetime.combine(row['scheduled_date'], row['scheduled_time'])
        return start + timedelta(minutes=row.get('duration_minutes') or 60)

    def _alternative(self, context, intent, plan, decision):
        after = plan.after
        current = datetime.combine(after.scheduled_date, after.scheduled_time)
        nearest, occupied, available_after = self._next_free(
            context.owner_id, current, after.duration.minutes,
            after.id if intent.action == Action.UPDATE else None,
        )
        context.trace.update({
            'requested_start': current.isoformat(),
            'requested_duration_minutes': after.duration.minutes,
            'requested_duration': after.duration.minutes,
            'occupied_intervals': occupied,
            'nearest_free_slot': nearest.isoformat() if nearest else None,
            'available_after': available_after.isoformat(),
        })
        if nearest is None:
            return self._working_window_clarification(
                decision, available_after,
            )
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
        first = last = requested.date()
        rows = self.repository.meetings_between(self.connection, owner, first, last)
        by_day, occupied = self._occupied(rows, exclude_id)
        requested_end = requested + timedelta(minutes=duration)
        conflicts = [item for item in by_day.get(requested.date(), ())
                     if item[0] < requested_end and item[1] > requested]
        if not conflicts:
            return None, None, occupied, requested
        candidate = max(requested, datetime.combine(first, WORKING_DAY_START))
        closing = datetime.combine(first, WORKING_DAY_END)
        candidate, available_after = self._first_free(
            candidate, closing, duration, by_day.get(first, ()),
        )
        return conflicts[0][2], candidate, occupied, available_after

    def _next_free(self, owner, current, duration, exclude_id):
        first = last = current.date()
        rows = self.repository.meetings_between(
            self.connection, owner, first, last,
        )
        by_day, occupied = self._occupied(rows, exclude_id)
        earliest = current + timedelta(minutes=30)
        opening = datetime.combine(first, WORKING_DAY_START)
        candidate = max(earliest, opening)
        closing = datetime.combine(first, WORKING_DAY_END)
        candidate, available_after = self._first_free(
            candidate, closing, duration, by_day.get(first, ()),
        )
        return candidate, occupied, available_after

    @classmethod
    def _occupied(cls, rows, exclude_id):
        by_day = {}
        raw = []
        for row in rows:
            if row['id'] == exclude_id:
                continue
            start = datetime.combine(row['scheduled_date'], row['scheduled_time'])
            end = row.get('reserved_until') or cls._reserved_until(row)
            by_day.setdefault(start.date(), []).append((start, end, row))
            raw.append({'id': str(row['id']), 'start': start.isoformat(),
                        'end': end.isoformat()})
        for day, intervals in by_day.items():
            merged = []
            for start, end, row in sorted(intervals, key=lambda item: item[0]):
                if merged and start <= merged[-1][1]:
                    old_start, old_end, old_row = merged[-1]
                    merged[-1] = (old_start, max(old_end, end), old_row)
                else:
                    merged.append((start, end, row))
            by_day[day] = merged
        return by_day, raw

    @staticmethod
    def _first_free(candidate, closing, duration, intervals):
        required = timedelta(minutes=duration)
        for start, end, *_ in intervals:
            if end <= candidate:
                continue
            if candidate + required <= start:
                return candidate, candidate
            if start < candidate + required:
                candidate = max(candidate, end)
            if candidate + required > closing:
                return None, candidate
        return ((candidate, candidate) if candidate + required <= closing
                else (None, candidate))

    @staticmethod
    def _working_window_clarification(decision, available_after):
        return decision.model_copy(update={
            'phase': Phase.NEED_FIELDS,
            'missing_fields': ('scheduled_date', 'scheduled_time'),
            'calendar_clarification': 'working_window',
            'question': (
                'В этот день свободное время есть только после '
                f'{available_after:%H:%M} или до {WORKING_DAY_START:%H:%M}. '
                'Когда вы хотите поставить встречу?'
            ),
        })
