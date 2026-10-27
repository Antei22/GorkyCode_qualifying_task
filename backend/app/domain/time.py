"""Injectable clock and explicit Russian date rules.

Never derives 'today' from the maximum date in an imported dataset.
"""
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo
from .models import DomainError

WEEKDAYS = {'понедельник': 0, 'вторник': 1, 'среду': 2, 'среда': 2,
            'четверг': 3, 'пятницу': 4, 'пятница': 4, 'субботу': 5,
            'суббота': 5, 'воскресенье': 6}
MONTHS = {'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5,
          'июня': 6, 'июля': 7, 'августа': 8, 'сентября': 9, 'октября': 10,
          'ноября': 11, 'декабря': 12}
NUMBERS = {'один': 1, 'одну': 1, 'два': 2, 'две': 2, 'три': 3, 'четыре': 4,
           'пять': 5, 'шесть': 6, 'семь': 7, 'восемь': 8, 'девять': 9,
           'десять': 10, 'одиннадцать': 11, 'двенадцать': 12}


class Clock:
    def __init__(self, timezone='Europe/Moscow', fixed_now=''):
        self.zone = ZoneInfo(timezone)
        self.fixed_now = fixed_now

    def now(self):
        if self.fixed_now:
            moment = datetime.fromisoformat(self.fixed_now)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=self.zone)
            return moment.astimezone(self.zone)
        return datetime.now(self.zone)

    def context(self):
        return f'Сейчас {self.now().isoformat()}, пояс {self.zone.key}. Неделя: понедельник–воскресенье.'


def parse_day(expression, now):
    value = (expression or '').strip().lower().replace('ё', 'е')
    if not value:
        return None
    offsets = {'сегодня': 0, 'завтра': 1, 'послезавтра': 2, 'вчера': -1}
    if value in offsets:
        return now.date() + timedelta(days=offsets[value])
    if value in ('до конца недели', 'конец недели', 'в конце недели'):
        return now.date() + timedelta(days=6 - now.weekday())
    if value in ('на следующей неделе', 'следующая неделя'):
        raise DomainError('Какой день следующей недели выбрать?')
    weekday = re.sub(r'^(в|во)\s+', '', value)
    if weekday in WEEKDAYS:
        return now.date() + timedelta(days=(WEEKDAYS[weekday] - now.weekday()) % 7)
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    match = re.fullmatch(r'(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?', value)
    if match and match[2] in MONTHS:
        try:
            return date(int(match[3] or now.year), MONTHS[match[2]], int(match[1]))
        except ValueError as error:
            raise DomainError('Такой календарной даты нет.') from error
    raise DomainError('Уточните дату: например, «завтра», «в пятницу» или YYYY-MM-DD.')


def parse_time(expression):
    value = (expression or '').strip().lower()
    if not value:
        return None
    value = re.sub(r'^в\s+', '', value)
    for word, number in NUMBERS.items():
        value = re.sub(rf'\b{word}\b', str(number), value)
    match = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?(?:\s*(утра|вечера|дня|ночи))?', value)
    if not match:
        raise DomainError('Уточните время, например 10:00 или «в 3 дня».')
    hour, minute = int(match[1]), int(match[2] or 0)
    if match[3] in ('вечера', 'дня') and 1 <= hour <= 11:
        hour += 12
    if match[3] == 'ночи' and hour == 12:
        hour = 0
    try:
        return time(hour, minute)
    except ValueError as error:
        raise DomainError('Время должно быть от 00:00 до 23:59.') from error


def schedule(day_expression, time_expression, now):
    value = (day_expression or '').strip().lower()
    for word, number in NUMBERS.items():
        value = re.sub(rf'\b{word}\b', str(number), value)
    match = re.fullmatch(r'через\s+(\d+)\s*(час|часа|часов|минуту|минуты|минут)', value)
    if match:
        minutes = int(match[1]) * (60 if match[2].startswith('час') else 1)
        moment = now + timedelta(minutes=minutes)
        return moment.date(), moment.time().replace(second=0, microsecond=0, tzinfo=None)
    return parse_day(day_expression, now), parse_time(time_expression)


def period_bounds(period, now, date_from='', date_to=''):
    day = now.date()
    if period == 'all':
        return None, None
    if period in ('today', 'tomorrow', 'day_after_tomorrow'):
        first = day + timedelta(days={'today': 0, 'tomorrow': 1, 'day_after_tomorrow': 2}[period])
        return first, first
    if period in ('this_week', 'next_week'):
        first = day - timedelta(days=day.weekday())
        if period == 'next_week':
            first += timedelta(days=7)
        return first, first + timedelta(days=6)
    if period != 'custom':
        raise DomainError('Неизвестный период.')
    first, last = parse_day(date_from, now), parse_day(date_to, now)
    if not first or not last or first > last:
        raise DomainError('Укажите корректные начало и конец периода.')
    return first, last

