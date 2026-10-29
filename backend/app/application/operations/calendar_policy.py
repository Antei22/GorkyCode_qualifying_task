"""Calendar conflict policy layered over the generic operation invariants."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta

from app.domain.operations import (
    Action, OperationIntent, Phase, RecordPatch, make_change_plan,
)
from .policy import DefaultOperationPolicy
from .state import merge_intent


WORK_HOURS = 'work_hours'
WEEKDAYS_EXTENDED_HOURS = 'weekdays_extended_hours'
ALL_DAYS_WORK_HOURS = 'all_days_work_hours'
EXTENDED_HOURS = 'extended_hours'
WEEKENDS_WORK_HOURS = 'weekends_work_hours'
WEEKENDS_EXTENDED_HOURS = 'weekends_extended_hours'


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
            intent.calendar_scope,
            context.now,
        )
        if conflict is None:
            return decision
        conflict_description = self._conflict_description(conflict)
        if intent.calendar_scope is None:
            return decision.model_copy(update={
                'phase': Phase.NEED_FIELDS,
                'missing_fields': ('calendar_scope',),
                'question': (
                    f'Выбранное время пересекается со встречей '
                    f'{conflict_description}. Где искать свободное время: только '
                    'по будням, только по выходным или в любой день; '
                    'с 08:00 до 18:00 либо с 08:00 до 22:00?'
                ),
            })
        if nearest is None:
            window = self._window_label(intent.calendar_scope)
            return decision.model_copy(update={
                'phase': Phase.NEED_FIELDS,
                'missing_fields': ('scheduled_date', 'scheduled_time'),
                'question': ('В ближайшие 30 дней нет подходящего свободного времени '
                             f'{window}. Укажите другую дату и время.'),
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
            f'Выбранное время пересекается со встречей {conflict_description}. '
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
            intent.calendar_scope or WORK_HOURS,
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

    def _nearest(self, owner, requested, duration, exclude_id, calendar_scope,
                 now):
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
        if calendar_scope is None:
            return conflicts[0][2], None
        opening_time, closing_time, day_mode = self._window(calendar_scope)
        for offset in range(31):
            day = first + timedelta(days=offset)
            if not self._day_allowed(day, day_mode):
                continue
            opening = datetime.combine(day, opening_time)
            candidate = opening
            if day == now.date():
                candidate = max(candidate, now.replace(tzinfo=None, second=0,
                                                       microsecond=0))
            closing = datetime.combine(day, closing_time)
            while candidate + timedelta(minutes=duration) <= closing:
                end = candidate + timedelta(minutes=duration)
                overlaps = [item for item in by_day.get(day, ())
                            if item[0] < end and item[1] > candidate]
                if not overlaps:
                    return conflicts[0][2], candidate
                candidate = max(item[1] for item in overlaps)
        return conflicts[0][2], None

    def _next_free(self, owner, current, duration, exclude_id, calendar_scope):
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
        # A different proposal must not overlap the previously proposed slot.
        earliest = current + timedelta(minutes=duration)
        opening_time, closing_time, day_mode = self._window(calendar_scope)
        for offset in range(31):
            day = first + timedelta(days=offset)
            if not self._day_allowed(day, day_mode):
                continue
            opening = datetime.combine(day, opening_time)
            candidate = max(earliest, opening) if day == first else opening
            closing = datetime.combine(day, closing_time)
            while candidate + timedelta(minutes=duration) <= closing:
                end = candidate + timedelta(minutes=duration)
                overlaps = [item for item in by_day.get(day, ())
                            if item[0] < end and item[1] > candidate]
                if not overlaps:
                    return candidate
                candidate = max(item[1] for item in overlaps)
        return None

    @staticmethod
    def _window(calendar_scope):
        closing = (time(22) if calendar_scope in (
            WEEKDAYS_EXTENDED_HOURS, EXTENDED_HOURS, WEEKENDS_EXTENDED_HOURS,
        ) else time(18))
        if calendar_scope in (WEEKENDS_WORK_HOURS, WEEKENDS_EXTENDED_HOURS):
            day_mode = 'weekends'
        elif calendar_scope in (ALL_DAYS_WORK_HOURS, EXTENDED_HOURS):
            day_mode = 'all'
        else:
            day_mode = 'weekdays'
        return time(8), closing, day_mode

    @staticmethod
    def _day_allowed(day, day_mode):
        if day_mode == 'all':
            return True
        return day.weekday() >= 5 if day_mode == 'weekends' else day.weekday() < 5

    @staticmethod
    def _window_label(calendar_scope):
        labels = {
            WORK_HOURS: 'по будням с 08:00 до 18:00',
            WEEKDAYS_EXTENDED_HOURS: 'по будням с 08:00 до 22:00',
            ALL_DAYS_WORK_HOURS: 'ежедневно с 08:00 до 18:00',
            EXTENDED_HOURS: 'ежедневно с 08:00 до 22:00',
            WEEKENDS_WORK_HOURS: 'по выходным с 08:00 до 18:00',
            WEEKENDS_EXTENDED_HOURS: 'по выходным с 08:00 до 22:00',
        }
        return labels[calendar_scope]

    @staticmethod
    def _conflict_description(conflict):
        start = datetime.combine(
            conflict['scheduled_date'], conflict['scheduled_time'],
        )
        end = start + timedelta(minutes=conflict.get('duration_minutes') or 60)
        if start.date() == end.date():
            interval = f'{start:%d.%m.%Y с %H:%M} до {end:%H:%M}'
        else:
            interval = f'с {start:%d.%m.%Y %H:%M} до {end:%d.%m.%Y %H:%M}'
        return f'«{conflict["text"]}», которая длится {interval}'
