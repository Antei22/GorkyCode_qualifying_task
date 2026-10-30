"""State-owned confirmation parser using only the current message."""
import json
import logging
from datetime import date
from app.domain.operations import TurnKind


logger = logging.getLogger(__name__)
KIND_LABELS = {
    'meeting': 'встречу', 'task': 'задачу',
    'reminder': 'напоминание', 'call': 'созвон',
}
KIND_GENITIVE = {
    'meeting': 'встречи', 'task': 'задачи',
    'reminder': 'напоминания', 'call': 'созвона',
}
KIND_NOMINATIVE = {
    'meeting': 'встреча', 'task': 'задача',
    'reminder': 'напоминание', 'call': 'созвон',
}
WEEKDAY_LABELS = {
    0: 'понедельник', 1: 'вторник', 2: 'среда', 3: 'четверг',
    4: 'пятница', 5: 'суббота', 6: 'воскресенье',
}


def format_date(value):
    return value.strftime('%d.%m.%Y') if value is not None else 'не указана'


def format_time(value):
    return value.strftime('%H:%M') if value is not None else 'не указано'


def format_datetime(day, clock):
    return f'{format_date(day)} {format_time(clock)}'


def format_weekday_date(value):
    day = value if isinstance(value, date) else date.fromisoformat(value)
    return f'{WEEKDAY_LABELS[day.weekday()]}, {format_date(day)}'


def format_duration(minutes, grammatical_case='nominative'):
    if minutes is None:
        return 'не указана'
    if minutes % 60 == 0:
        hours = minutes // 60
        if grammatical_case == 'genitive':
            suffix = 'часа' if hours == 1 else 'часов'
        else:
            suffix = 'час' if hours == 1 else 'часа' if 2 <= hours <= 4 else 'часов'
        return f'{hours} {suffix}'
    hours, remainder = divmod(minutes, 60)
    return (f'{hours} ч {remainder} мин.' if hours else f'{remainder} мин.')


def update_changed_fields(before, after):
    fields = []
    for name, old, new in (
        ('date', before.scheduled_date, after.scheduled_date),
        ('time', before.scheduled_time, after.scheduled_time),
        ('duration', before.duration.minutes, after.duration.minutes),
        ('text', before.text, after.text),
        ('kind', before.kind, after.kind),
    ):
        if old != new:
            fields.append((name, old, new))
    return fields


def build_update_confirmation(before, after):
    """Render the exact server-side before/after diff without an LLM."""
    changes = update_changed_fields(before, after)
    label = KIND_LABELS.get(after.kind, 'запись')
    genitive_label = KIND_GENITIVE.get(after.kind, 'записи')
    title = f'«{after.text}»'
    names = {item[0] for item in changes}
    if names == {'date'}:
        message = (
            f'Вы действительно хотите перенести {label} {title} '
            f'с {format_date(before.scheduled_date)} '
            f'на {format_date(after.scheduled_date)}?'
        )
    elif names == {'time'}:
        message = (
            f'Вы действительно хотите изменить время {genitive_label} {title} '
            f'с {format_time(before.scheduled_time)} '
            f'на {format_time(after.scheduled_time)}?'
        )
    elif names == {'date', 'time'}:
        message = (
            f'Вы действительно хотите перенести {label} {title} '
            f'с {format_datetime(before.scheduled_date, before.scheduled_time)} '
            f'на {format_datetime(after.scheduled_date, after.scheduled_time)}?'
        )
    elif names == {'duration'}:
        message = (
            f'Вы действительно хотите изменить длительность {genitive_label} {title} '
            f'с {format_duration(before.duration.minutes, "genitive")} '
            f'на {format_duration(after.duration.minutes)}?'
        )
    else:
        lines = []
        labels = {
            'date': 'Дата', 'time': 'Время', 'duration': 'Длительность',
            'text': 'Название', 'kind': 'Тип',
        }
        formatters = {
            'date': format_date, 'time': format_time,
            'duration': format_duration, 'text': str,
            'kind': lambda value: KIND_NOMINATIVE.get(value, value or 'не указан'),
        }
        for name, old, new in changes:
            formatter = formatters[name]
            lines.append(f'{labels[name]}: {formatter(old)} → {formatter(new)}')
        message = (
            f'Вы действительно хотите изменить {label} {title}?\n\n'
            + '\n'.join(lines)
        )
    logger.info('update_confirmation %s', json.dumps({
        'before': before.model_dump(mode='json'),
        'after': after.model_dump(mode='json'),
        'changed_fields': [item[0] for item in changes],
        'confirmation_message': message,
    }, ensure_ascii=False))
    return message


def build_pending_change_confirmation(kind, old_values, new_values):
    """Describe a conflicting follow-up patch using only canonical values."""
    label = KIND_LABELS.get(kind, 'запись')
    genitive_label = KIND_GENITIVE.get(kind, 'записи')
    old_date = old_values.get('date_expression')
    new_date = new_values.get('date_expression')
    final_time = new_values.get('time_expression')
    if old_date is not None and new_date is not None:
        destination = f'{format_date(date.fromisoformat(new_date))}'
        if final_time:
            destination += f' в {final_time}'
        return (
            f'Вы ранее указали {format_weekday_date(old_date)},\n'
            f'а сейчас — {format_weekday_date(new_date)}.\n\n'
            f'Перенести {label} на {destination}?'
        )
    old_time = old_values.get('time_expression')
    new_time = new_values.get('time_expression')
    if old_time is not None and new_time is not None:
        return (
            f'Вы ранее указали время {old_time},\n'
            f'а сейчас — {new_time}.\n\n'
            f'Изменить время {genitive_label} на {new_time}?'
        )
    labels = {
        'kind': 'Тип', 'text': 'Текст', 'date_expression': 'Дата',
        'time_expression': 'Время', 'timezone': 'Часовой пояс',
        'duration': 'Длительность',
    }
    lines = [
        f'{labels.get(field, field)}: {old_values[field]} → {new_values[field]}'
        for field in old_values if field in new_values
    ]
    return f'Изменить {label}?\n\n' + '\n'.join(lines)


class ConfirmationResolver:
    CONFIRM = frozenset({
        'да', 'ага', 'подтверждаю', 'подтвердить', 'давай', 'верно',
    })
    REJECT = frozenset({
        'нет', 'не надо', 'отмена', 'оставь как было',
    })

    @classmethod
    def resolve(cls, message: str) -> TurnKind:
        value = ' '.join(message.lower().replace('ё', 'е').split()).strip(' .,!')
        if value in cls.CONFIRM:
            return TurnKind.CONFIRM
        if value in cls.REJECT:
            return TurnKind.REJECT
        return TurnKind.UNKNOWN
