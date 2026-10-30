"""Injectable clock and explicit Russian date rules.

Never derives 'today' from the maximum date in an imported dataset.
"""
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from typing import Annotated, Literal
from zoneinfo import ZoneInfo
from pydantic import Field
from pydantic import BaseModel, ConfigDict
from .models import DomainError, FieldInputError

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


class ParsedTemporal(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    evidence: str | None = None


class AbsoluteDate(ParsedTemporal):
    type: Literal['absolute']
    year: int = Field(ge=1, le=9999)
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)


class RelativeDate(ParsedTemporal):
    type: Literal['relative']
    days_offset: int = Field(ge=-3660, le=3660)


class MonthDayDate(ParsedTemporal):
    type: Literal['month_day']
    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)
    year: None = None
    resolution: Literal['nearest_future'] = 'nearest_future'


class WeekdayDate(ParsedTemporal):
    type: Literal['weekday']
    weekday: int = Field(ge=0, le=6)
    week_offset: int = Field(ge=0, le=52)


class PeriodDate(ParsedTemporal):
    type: Literal['end_of_period', 'start_of_period']
    period: Literal['week', 'month']
    period_offset: int = Field(ge=0, le=52)


class PeriodExpression(ParsedTemporal):
    type: Literal['period']
    unit: Literal['week', 'month']
    offset: int = Field(default=0, ge=0, le=52)
    mode: Literal['whole', 'until_end', 'from_start'] = 'whole'


DateRangePoint = AbsoluteDate | MonthDayDate


class DateRange(ParsedTemporal):
    type: Literal['date_range']
    start: DateRangePoint
    end: DateRangePoint


ParsedDate = Annotated[
    AbsoluteDate | RelativeDate | MonthDayDate | WeekdayDate | PeriodDate
    | PeriodExpression | DateRange,
    Field(discriminator='type'),
]


class ParsedTime(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    type: Literal['absolute'] = 'absolute'
    hour: int = Field(ge=0, le=23)
    minute: int = Field(ge=0, le=59)
    evidence: str | None = None


def normalize_date_payload(value):
    """Normalize one common date patch for every record kind.

    Only the fields belonging to the declared discriminator survive. A missing
    relative offset may be recovered from its exact evidence token.
    """
    if not isinstance(value, dict):
        return value
    kind = value.get('type')
    evidence = value.get('evidence')
    normalized_evidence = (evidence or '').lower().replace('ё', 'е').strip(' .,!?')
    offsets = {'сегодня': 0, 'завтра': 1, 'послезавтра': 2, 'вчера': -1,
               'на сегодня': 0, 'на завтра': 1, 'на послезавтра': 2}
    # Deterministic semantic guard: these tokens can never be weekdays or
    # absolute dates, regardless of what the model emitted.
    if normalized_evidence in offsets:
        return {
            'type': 'relative', 'days_offset': offsets[normalized_evidence],
            **({'evidence': evidence} if evidence is not None else {}),
        }
    weekday_evidence = re.sub(
        r'^(?:в|во|на)\s+', '', normalized_evidence,
    )
    next_week = bool(re.match(r'^следующ\w*\s+', weekday_evidence))
    weekday_evidence = re.sub(r'^следующ\w*\s+', '', weekday_evidence)
    if weekday_evidence in WEEKDAYS:
        return {
            'type': 'weekday', 'weekday': WEEKDAYS[weekday_evidence],
            'week_offset': 1 if next_week else value.get('week_offset', 0),
            **({'evidence': evidence} if evidence is not None else {}),
        }
    if kind in {'start_of_period', 'end_of_period'}:
        return {
            'type': 'period',
            'unit': value.get('period'),
            'offset': value.get('period_offset', 0),
            'mode': ('until_end' if kind == 'end_of_period' else 'whole'),
            **({'evidence': evidence} if evidence is not None else {}),
        }
    allowed = {
        'absolute': ('type', 'year', 'month', 'day', 'evidence'),
        'relative': ('type', 'days_offset', 'evidence'),
        'month_day': ('type', 'month', 'day', 'year', 'resolution', 'evidence'),
        'weekday': ('type', 'weekday', 'week_offset', 'evidence'),
        'start_of_period': ('type', 'period', 'period_offset', 'evidence'),
        'end_of_period': ('type', 'period', 'period_offset', 'evidence'),
        'period': ('type', 'unit', 'offset', 'mode', 'evidence'),
        'date_range': ('type', 'start', 'end', 'evidence'),
    }.get(kind)
    if allowed is None:
        return value
    result = {field: value[field] for field in allowed if field in value}
    if (kind == 'relative' and result.get('days_offset') is None
            and normalized_evidence in offsets):
        result['days_offset'] = offsets[normalized_evidence]
    if kind == 'month_day':
        result.setdefault('year', None)
        result.setdefault('resolution', 'nearest_future')
    if kind == 'weekday':
        result.setdefault('week_offset', 0)
    if kind in {'start_of_period', 'end_of_period'}:
        result.setdefault('period_offset', 0)
    if kind == 'period':
        result.setdefault('offset', 0)
        result.setdefault('mode', 'whole')
    if kind == 'date_range':
        result['start'] = normalize_date_payload(result.get('start'))
        result['end'] = normalize_date_payload(result.get('end'))
    return result


class TemporalBounds(BaseModel):
    model_config = ConfigDict(frozen=True)
    start: date
    end: date

    @property
    def end_exclusive(self):
        return self.end + timedelta(days=1)


def resolve_temporal(value: ParsedDate, now: datetime) -> TemporalBounds:
    """Resolve a point, period or date range without losing range semantics."""
    if isinstance(value, PeriodExpression):
        if value.unit == 'week':
            period_start = now.date() - timedelta(days=now.weekday())
            period_start += timedelta(weeks=value.offset)
            period_end = period_start + timedelta(days=6)
        else:
            month_index = now.year * 12 + now.month - 1 + value.offset
            year, month_zero = divmod(month_index, 12)
            month = month_zero + 1
            period_start = date(year, month, 1)
            period_end = date(year, month, monthrange(year, month)[1])
        start = (now.date() if value.mode == 'until_end' and value.offset == 0
                 else period_start)
        return TemporalBounds(start=start, end=period_end)
    if isinstance(value, DateRange):
        start = _resolve_range_point(value.start, now, None)
        end = _resolve_range_point(value.end, now, start)
        return TemporalBounds(start=start, end=end)
    if isinstance(value, PeriodDate):
        start = now.date() - timedelta(days=now.weekday())
        if value.period == 'week':
            start += timedelta(weeks=value.period_offset)
            end = start + timedelta(days=6)
        else:
            month_index = now.year * 12 + now.month - 1 + value.period_offset
            year, month_zero = divmod(month_index, 12)
            month = month_zero + 1
            start = date(year, month, 1)
            end = date(year, month, monthrange(year, month)[1])
        if value.type == 'start_of_period':
            end = start
        else:
            start = end
        return TemporalBounds(start=start, end=end)
    point = resolve_date(value, now)
    return TemporalBounds(start=point, end=point)


def _resolve_range_point(value: DateRangePoint, now: datetime,
                         not_before: date | None) -> date:
    if isinstance(value, AbsoluteDate):
        result = resolve_date(value, now)
        if not_before is not None and result < not_before:
            raise FieldInputError('date_expression', 'Конец диапазона раньше начала.')
        return result
    first_year = not_before.year if not_before is not None else now.year
    for year in range(first_year, first_year + 9):
        try:
            candidate = date(year, value.month, value.day)
        except ValueError:
            continue
        threshold = not_before or now.date()
        if candidate >= threshold:
            return candidate
    raise FieldInputError('date_expression', 'Такой календарной даты нет.')


def normalize_time_payload(value):
    """Normalize the shared time patch without interpreting record kind."""
    if not isinstance(value, dict):
        return value
    result = {field: value[field] for field in (
        'type', 'hour', 'minute', 'evidence'
    ) if field in value}
    result.setdefault('type', 'absolute')
    result.setdefault('minute', 0)
    return result


def resolve_date(value: ParsedDate, now: datetime) -> date:
    """Resolve LLM semantic date data; never parses user-facing date text."""
    if isinstance(value, AbsoluteDate):
        try:
            return date(value.year, value.month, value.day)
        except ValueError as error:
            raise FieldInputError('date_expression', 'Такой календарной даты нет.') from error
    if isinstance(value, RelativeDate):
        return now.date() + timedelta(days=value.days_offset)
    if isinstance(value, MonthDayDate):
        for year in range(now.year, now.year + 9):
            try:
                candidate = date(year, value.month, value.day)
            except ValueError:
                continue
            if candidate >= now.date():
                return candidate
        raise FieldInputError('date_expression', 'Такой календарной даты нет.')
    if isinstance(value, WeekdayDate):
        monday = now.date() - timedelta(days=now.weekday())
        target = monday + timedelta(days=value.week_offset * 7 + value.weekday)
        if value.week_offset == 0 and target < now.date():
            target += timedelta(days=7)
        return target
    if isinstance(value, PeriodDate):
        first = now.date().replace(day=1)
        if value.period == 'week':
            start = now.date() - timedelta(days=now.weekday())
            start += timedelta(weeks=value.period_offset)
            return (start + timedelta(days=6) if value.type == 'end_of_period'
                    else start)
        month_index = first.month - 1 + value.period_offset
        year, month = first.year + month_index // 12, month_index % 12 + 1
        start = date(year, month, 1)
        if value.type == 'start_of_period':
            return start
        return date(year, month, monthrange(year, month)[1])
    raise FieldInputError('date_expression', 'Не удалось разобрать дату.')


def resolve_time(value: ParsedTime) -> time:
    return time(value.hour, value.minute)


def parse_day(expression, now):
    value = (expression or '').strip().lower().replace('ё', 'е')
    if not value:
        return None
    offsets = {'сегодня': 0, 'завтра': 1, 'послезавтра': 2, 'вчера': -1}
    if value in offsets:
        return now.date() + timedelta(days=offsets[value])
    if value in ('до конца недели', 'конец недели', 'в конце недели'):
        return now.date() + timedelta(days=6 - now.weekday())
    if value in ('до конца месяца', 'конец месяца', 'в конце месяца'):
        return date(now.year, now.month, monthrange(now.year, now.month)[1])
    if value in ('на следующей неделе', 'следующая неделя'):
        raise FieldInputError('date_expression', 'Какой день следующей недели выбрать?')
    next_weekday = re.fullmatch(r'(?:в|во)?\s*следующ(?:ий|ую|ее)\s+([а-я]+)', value)
    if next_weekday and next_weekday[1] in WEEKDAYS:
        next_monday = now.date() + timedelta(days=7 - now.weekday())
        return next_monday + timedelta(days=WEEKDAYS[next_weekday[1]])
    weekday = re.sub(r'^(в|во)\s+', '', value)
    if weekday in WEEKDAYS:
        return now.date() + timedelta(days=(WEEKDAYS[weekday] - now.weekday()) % 7)
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    match = re.fullmatch(r'(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?', value)
    if match and match[2] in MONTHS:
        if match[3]:
            try:
                return date(int(match[3]), MONTHS[match[2]], int(match[1]))
            except ValueError as error:
                raise FieldInputError('date_expression', 'Такой календарной даты нет.') from error
        for year in range(now.year, now.year + 9):
            try:
                result = date(year, MONTHS[match[2]], int(match[1]))
            except ValueError:
                continue
            if result >= now.date():
                return result
        raise FieldInputError('date_expression', 'Такой календарной даты нет.')
    raise FieldInputError(
        'date_expression',
        'Уточните дату: например, «завтра», «в пятницу» или YYYY-MM-DD.',
    )


def parse_time(expression):
    value = (expression or '').strip().lower()
    if not value:
        return None
    value = re.sub(r'^в\s+', '', value)
    for word, number in NUMBERS.items():
        value = re.sub(rf'\b{word}\b', str(number), value)
    match = re.fullmatch(r'(\d{1,2})(?::(\d{2}))?(?:\s*(утра|вечера|дня|ночи))?', value)
    if not match:
        raise FieldInputError(
            'time_expression', 'Уточните время, например 10:00 или «в 3 дня».'
        )
    hour, minute = int(match[1]), int(match[2] or 0)
    if match[3] in ('вечера', 'дня') and 1 <= hour <= 11:
        hour += 12
    if match[3] == 'ночи' and hour == 12:
        hour = 0
    try:
        return time(hour, minute)
    except ValueError as error:
        raise FieldInputError(
            'time_expression', 'Время должно быть от 00:00 до 23:59.'
        ) from error


def schedule(day_expression, time_expression, now):
    value = (day_expression or '').strip().lower()
    if value == 'через полчаса':
        moment = now + timedelta(minutes=30)
        return moment.date(), moment.time().replace(second=0, microsecond=0, tzinfo=None)
    if value in ('через неделю', 'через одну неделю'):
        moment = now + timedelta(days=7)
        return moment.date(), moment.time().replace(second=0, microsecond=0, tzinfo=None)
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

