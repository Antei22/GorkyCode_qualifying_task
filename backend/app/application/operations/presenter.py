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
            return (f'{label}: «{record.text}»{self._when(record)}.'
                    f'{self._saved_duration(record)}')
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
        if (record.scheduled_end_date is not None
                and record.scheduled_end_date != record.scheduled_date):
            value += f'–{record.scheduled_end_date:%d.%m.%Y}'
        if record.scheduled_time is not None:
            value += f' в {record.scheduled_time:%H:%M}'
        return f' — {value}'

    @classmethod
    def _duration_line(cls, record, context):
        label = f'«{record.text}»{cls._when(record)}'
        user = record.user_duration
        if user and user.type == 'exact':
            detail = f'длительность — {cls._minutes(user.minutes)}.'
        elif user and user.type == 'approximate':
            detail = f'рассчитана примерно на {cls._minutes(user.minutes)}.'
        elif user and user.type == 'range':
            detail = (
                'рассчитана примерно на '
                f'{cls._range(user.min_minutes, user.max_minutes)}.'
            )
        elif user and user.type == 'qualitative':
            wording = 'долгая' if user.value == 'long' else 'недолгая'
            detail = f'указано только, что встреча {wording}.'
        else:
            detail = 'точная длительность не указана.'
        if record.duration.minutes is not None and (
                user is None or user.type == 'qualitative'):
            detail += (
                ' Для расписания зарезервировано '
                f'{cls._minutes(record.duration.minutes)}.'
            )
        if record.scheduled_date is not None:
            from datetime import datetime, time
            from zoneinfo import ZoneInfo
            scheduled = datetime.combine(
                record.scheduled_date, record.scheduled_time or time.max,
            ).replace(tzinfo=ZoneInfo(record.timezone))
            if scheduled < context.now:
                detail += ' Фактическая продолжительность не зафиксирована.'
        return f'{label}: {detail}'

    @classmethod
    def _saved_duration(cls, record):
        if record.kind != 'meeting':
            return ''
        user = record.user_duration
        if user and user.type == 'exact':
            return f' Длительность — {cls._minutes(user.minutes)}.'
        if user and user.type == 'approximate':
            return f' Примерная длительность — {cls._minutes(user.minutes)}.'
        if user and user.type == 'range':
            return f' Примерная длительность — {cls._range(user.min_minutes, user.max_minutes)}.'
        if user and user.type == 'qualitative':
            wording = 'долгая' if user.value == 'long' else 'недолгая'
            reserve = cls._minutes(record.duration.minutes)
            return (f' Указано, что встреча {wording}; '
                    f'для расписания зарезервировано {reserve}.')
        if record.duration_status in {'declined_to_specify', 'defaulted'}:
            reserve = cls._minutes(record.duration.minutes)
            return (f' Точная длительность не указана; '
                    f'для расписания зарезервировано {reserve}.')
        return ''

    @staticmethod
    def _minutes(value):
        if value is None:
            return 'неизвестное время'
        if value % 60 == 0:
            hours = value // 60
            suffix = 'час' if hours == 1 else 'часа' if 2 <= hours <= 4 else 'часов'
            return f'{hours} {suffix}'
        return f'{value} мин.'

    @staticmethod
    def _range(low, high):
        if low % 60 == 0 and high % 60 == 0:
            low_hours, high_hours = low // 60, high // 60
            suffix = ('час' if high_hours == 1 else
                      'часа' if 2 <= high_hours <= 4 else 'часов')
            return f'{low_hours}–{high_hours} {suffix}'
        return f'{low}–{high} мин.'
