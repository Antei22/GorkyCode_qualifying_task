"""Deterministic presentation of server-owned operation facts."""
from dataclasses import dataclass

from app.domain.operations import Action, OperationOutcome, Phase
from .ports import TurnContext


_CREATED = {'task': 'Задача сохранена', 'meeting': 'Встреча сохранена',
            'reminder': 'Напоминание сохранено', None: 'Запись сохранена'}
_UPDATED = {'task': 'Задача изменена', 'meeting': 'Встреча изменена',
            'reminder': 'Напоминание изменено', None: 'Запись изменена'}
_CANCELLED = {'task': 'Задача отменена', 'meeting': 'Встреча отменена',
              'reminder': 'Напоминание отменено', None: 'Запись отменена'}


@dataclass(frozen=True)
class DefaultOutcomePresenter:
    max_records: int = 10

    def present(self, context: TurnContext, outcome: OperationOutcome) -> str:
        if outcome.question:
            if outcome.records:
                return outcome.question + '\n' + self._record_lines(outcome.records)
            return outcome.question
        if outcome.phase == Phase.REJECTED:
            return (outcome.issues[0].message if outcome.issues
                    else 'Операция не выполнена.')
        if outcome.phase != Phase.COMPLETED:
            return 'Уточните параметры операции.'
        if outcome.execution is not None:
            record = outcome.execution.records[0]
            if outcome.execution.action == Action.CANCEL.value:
                return f'{_CANCELLED[record.kind]}: «{record.text}».'
            label = (_CREATED[record.kind] if outcome.execution.action == Action.CREATE.value
                     else _UPDATED[record.kind])
            return f'{label}: «{record.text}»{self._when(record)}.'
        if not outcome.records:
            return 'Подходящих записей не найдено.'
        if 'duration' in outcome.read_fields:
            return '\n'.join(self._duration_line(item, context) for item in outcome.records)
        return self._record_lines(outcome.records)

    def _record_lines(self, records):
        lines = [f'{index}. {item.text}{self._when(item)}'
                 for index, item in enumerate(records[:self.max_records], 1)]
        if len(records) > self.max_records:
            lines.append('Уточните запрос, чтобы сократить список.')
        return '\n'.join(lines)

    @staticmethod
    def _when(record):
        if record.scheduled_date is None:
            return ''
        value = record.scheduled_date.strftime('%d.%m.%Y')
        if record.scheduled_time is not None:
            value += f' в {record.scheduled_time:%H:%M}'
        return f' — {value}'

    @classmethod
    def _duration_line(cls, record, context):
        label = f'«{record.text}»{cls._when(record)}'
        if record.duration.source == 'user' and record.duration.minutes is not None:
            detail = f'указанная плановая длительность — {record.duration.minutes} мин.'
        else:
            detail = 'точная длительность не указана.'
            if record.duration.description and 'долго' in record.duration.description.lower():
                detail += ' Указано только, что встреча долгая.'
        if record.scheduled_date is not None:
            from datetime import datetime, time
            from zoneinfo import ZoneInfo
            scheduled = datetime.combine(
                record.scheduled_date, record.scheduled_time or time.max,
            ).replace(tzinfo=ZoneInfo(record.timezone))
            if scheduled < context.now:
                detail += ' Фактическая продолжительность не зафиксирована.'
        return f'{label}: {detail}'
