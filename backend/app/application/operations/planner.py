"""Default pure planner: patch plus current snapshot becomes one complete plan."""
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.domain.models import DomainError
from app.domain.operations import (
    Action, ChangePlan, DurationInput, DurationValue, OperationIntent,
    RecordSnapshot, make_change_plan,
)
from datetime import date, time
from app.domain.time import parse_day, parse_time, resolve_temporal
from app.domain.text import build_canonical_text
from .ports import TurnContext


class TaskPlanner:
    @staticmethod
    def apply_temporal(values, bounds):
        values['scheduled_date'] = bounds.start
        values['scheduled_end_date'] = bounds.end


class MeetingPlanner:
    @staticmethod
    def apply_temporal(values, bounds):
        values['scheduled_date'] = bounds.start if bounds.start == bounds.end else None
        values['scheduled_end_date'] = None


class ReminderPlanner(MeetingPlanner):
    pass


ENTITY_PLANNERS = {
    'task': TaskPlanner,
    'meeting': MeetingPlanner,
    'reminder': ReminderPlanner,
}


@dataclass(frozen=True)
class DefaultChangePlanner:
    record_id_factory: Callable[[], UUID] = uuid4

    def prepare(self, context: TurnContext, intent: OperationIntent,
                target: RecordSnapshot | None,
                previous_plan: ChangePlan | None = None) -> ChangePlan:
        if intent.action not in (Action.CREATE, Action.UPDATE, Action.CANCEL):
            raise DomainError('Для этого действия нельзя подготовить изменение.')
        if intent.action == Action.CREATE and target is not None:
            raise DomainError('Создание не может изменять существующую запись.')
        if intent.action != Action.CREATE and target is None:
            raise DomainError('Сначала выберите существующую запись.')

        if target is not None:
            before = target
            values = target.model_dump()
        else:
            before = None
            previous = (previous_plan.after if previous_plan is not None
                        and previous_plan.action == 'create' else None)
            record_id = previous.id if previous else self.record_id_factory()
            zone = getattr(context.now.tzinfo, 'key', None) or str(context.now.tzinfo)
            values = RecordSnapshot(
                id=record_id, kind=None, text='', scheduled_date=None,
                scheduled_end_date=None,
                scheduled_time=None, timezone=zone,
                user_duration=None, duration_status='defaulted',
                duration=DurationValue(), status='open', revision='new',
            ).model_dump()

        patch = intent.changes
        supplied = patch.model_fields_set
        unknown = set(intent.unknown_fields)
        if intent.action != Action.CANCEL:
            if 'kind' in supplied:
                values['kind'] = patch.kind
                values['kind_source'] = intent.kind_source or 'user_input'
            if 'text' in supplied:
                evidence = tuple(intent.field_evidence.values())
                values['text'] = (build_canonical_text(
                    values.get('kind'), patch.text,
                    intent.source_text or patch.text, evidence,
                ) if patch.text else '')
            if 'timezone' in supplied:
                values['timezone'] = patch.timezone or ''
            self._apply_schedule(values, patch, supplied, context)
            if 'duration' in supplied:
                user_duration, blocking, duration_status = self._duration(
                    patch.duration, values['text'], values.get('kind'),
                )
                values['user_duration'] = user_duration
                values['duration'] = blocking
                values['duration_status'] = duration_status
            elif 'duration' in unknown:
                values['user_duration'] = None
                values['duration'] = self._default_duration(
                    values['text'], values.get('kind'),
                )
                values['duration_status'] = 'declined_to_specify'
            elif (intent.action == Action.CREATE and values.get('kind') == 'meeting'
                  and values['duration'].get('minutes') is None):
                # Missing user duration is not a missing required fact.  The
                # calendar reserves a transparent event-type default instead.
                values['user_duration'] = None
                values['duration'] = self._default_duration(
                    values['text'], values.get('kind'),
                )
                values['duration_status'] = 'missing'
        else:
            values['status'] = 'cancelled'

        after = RecordSnapshot.model_validate(values)
        plan = make_change_plan(intent.action.value, before, after)
        context.trace['canonical_text'] = after.text
        context.trace['planner_result'] = plan.model_dump(mode='json')
        return plan

    @staticmethod
    def _apply_schedule(values, patch, supplied, context):
        has_day = 'date_expression' in supplied
        has_temporal = 'temporal' in supplied and patch.temporal is not None
        has_time = 'time_expression' in supplied
        if has_temporal:
            bounds = resolve_temporal(patch.temporal, context.now)
            entity_planner = ENTITY_PLANNERS.get(values.get('kind'), MeetingPlanner)
            entity_planner.apply_temporal(values, bounds)
        if has_day and patch.date_expression:
            try:
                day = date.fromisoformat(patch.date_expression)
            except ValueError:
                # Compatibility for operation drafts created before the
                # structured temporal contract was introduced.
                day = parse_day(patch.date_expression, context.now)
            values['scheduled_date'] = day
            values['scheduled_end_date'] = None
            if has_time:
                if not patch.time_expression:
                    values['scheduled_time'] = None
                else:
                    try:
                        values['scheduled_time'] = time.fromisoformat(
                            patch.time_expression,
                        )
                    except ValueError:
                        values['scheduled_time'] = parse_time(patch.time_expression)
        elif has_day:
            values['scheduled_date'] = None
            values['scheduled_end_date'] = None
        if has_time and not has_day:
            if not patch.time_expression:
                values['scheduled_time'] = None
            else:
                try:
                    values['scheduled_time'] = time.fromisoformat(
                        patch.time_expression,
                    )
                except ValueError:
                    values['scheduled_time'] = parse_time(patch.time_expression)
        elif has_time and not patch.time_expression:
            values['scheduled_time'] = None

    @staticmethod
    def _duration(value: DurationInput | None, text: str, kind: str | None):
        if value is None:
            return (None, DefaultChangePlanner._default_duration(text, kind),
                    'missing')
        user = value.model_dump(exclude_none=True)
        duration_type = value.type
        if duration_type == 'exact':
            blocking = DurationValue(
                minutes=value.minutes, description=value.evidence,
                source='user', blocking_source='user_exact',
            )
        elif duration_type == 'approximate':
            blocking = DurationValue(
                minutes=value.minutes, description=value.evidence,
                source='estimated', blocking_source='user_approximate',
            )
        elif duration_type == 'range':
            blocking = DurationValue(
                minutes=value.max_minutes, description=value.evidence,
                source='estimated', blocking_source='user_range_max',
            )
        else:
            minutes = 120 if value.value == 'long' else 30
            blocking = DurationValue(
                minutes=minutes, description=value.evidence or value.description,
                source='estimated', blocking_source='qualitative_default',
            )
        return user, blocking, 'provided'

    @staticmethod
    def _default_duration(text: str, kind: str | None) -> DurationValue:
        haystack = text.lower().replace('ё', 'е')
        if any(word in haystack for word in (
                'конференц', 'воркшоп', 'тренинг', 'презентац')):
            minutes = 120
        elif any(word in haystack for word in (
                'деловая', 'совещан', 'переговор', 'интервью')):
            minutes = 90
        elif any(word in haystack for word in ('созвон', 'звонок', 'позвон')):
            minutes = 30
        else:
            minutes = 60
        return DurationValue(
            minutes=minutes, source='estimated',
            blocking_source='event_type_default',
        )
