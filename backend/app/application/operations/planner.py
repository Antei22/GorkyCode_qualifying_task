"""Default pure planner: patch plus current snapshot becomes one complete plan."""
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID, uuid4

from app.domain.models import DomainError
from app.domain.operations import (
    Action, ChangePlan, DurationInput, DurationValue, OperationIntent,
    RecordSnapshot, make_change_plan,
)
from app.domain.time import parse_time, schedule
from .ports import TurnContext


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
                scheduled_time=None, timezone=zone,
                duration=DurationValue(), status='open', revision='new',
            ).model_dump()

        patch = intent.changes
        supplied = patch.model_fields_set
        unknown = set(intent.unknown_fields)
        if intent.action != Action.CANCEL:
            if 'kind' in supplied:
                values['kind'] = patch.kind
            if 'text' in supplied:
                values['text'] = patch.text or ''
            if 'timezone' in supplied:
                values['timezone'] = patch.timezone or ''
            self._apply_schedule(values, patch, supplied, context)
            if 'duration' in supplied:
                values['duration'] = self._duration(patch.duration, values['text'])
            elif 'duration' in unknown:
                values['duration'] = self._duration(
                    DurationInput(description='точная длительность пользователю неизвестна'),
                    values['text'],
                )
        else:
            values['status'] = 'cancelled'

        after = RecordSnapshot.model_validate(values)
        return make_change_plan(intent.action.value, before, after)

    @staticmethod
    def _apply_schedule(values, patch, supplied, context):
        has_day = 'date_expression' in supplied
        has_time = 'time_expression' in supplied
        if has_day and patch.date_expression:
            day, clock = schedule(
                patch.date_expression,
                patch.time_expression if has_time else None,
                context.now,
            )
            values['scheduled_date'] = day
            if has_time or clock is not None:
                values['scheduled_time'] = clock
        elif has_day:
            values['scheduled_date'] = None
        if has_time and not has_day:
            values['scheduled_time'] = (parse_time(patch.time_expression)
                                        if patch.time_expression else None)
        elif has_time and not patch.time_expression:
            values['scheduled_time'] = None

    @staticmethod
    def _duration(value: DurationInput | None, text: str) -> DurationValue:
        if value is None:
            return DurationValue()
        if value.minutes is None and value.description:
            haystack = (text + ' ' + value.description).lower().replace('ё', 'е')
            if any(word in haystack for word in ('конференц', 'воркшоп', 'тренинг',
                                                  'презентац', 'долго')):
                minutes = 120
            elif any(word in haystack for word in ('деловая', 'совещан', 'переговор',
                                                    'интервью')):
                minutes = 90
            elif any(word in haystack for word in ('созвон', 'звонок', 'позвон')):
                minutes = 30
            else:
                minutes = 60
            return DurationValue(minutes=minutes, description=value.description,
                                 source='estimated')
        return DurationValue(
            minutes=value.minutes,
            description=value.description,
            source='user' if value.minutes is not None else 'unknown',
        )
