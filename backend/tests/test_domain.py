import unittest
import os
from datetime import date, time
from pathlib import Path
from app.domain.models import DomainError
from app.domain.time import (
    AbsoluteDate, Clock, DateRange, MonthDayDate, ParsedTime, PeriodDate,
    PeriodExpression, RelativeDate,
    WeekdayDate, parse_day, parse_time, period_bounds, resolve_date,
    resolve_time, schedule, normalize_date_payload, resolve_temporal,
)
from app.ingestion.csv_import import read_csv


class TimeTests(unittest.TestCase):
    def setUp(self):
        self.now = Clock(fixed_now='2026-10-27T12:00:00+03:00').now()

    def test_relative_dates_and_weekday(self):
        self.assertEqual(parse_day('завтра', self.now), date(2026, 10, 28))
        self.assertEqual(parse_day('послезавтра', self.now), date(2026, 10, 29))
        self.assertEqual(parse_day('в пятницу', self.now), date(2026, 10, 30))
        self.assertEqual(parse_day('до конца недели', self.now), date(2026, 11, 1))

    def test_structured_date_resolution(self):
        self.assertEqual(resolve_date(AbsoluteDate(
            type='absolute', year=2026, month=11, day=20,
            evidence='20.11.2026',
        ), self.now), date(2026, 11, 20))
        self.assertEqual(resolve_date(RelativeDate(
            type='relative', days_offset=1, evidence='завтра',
        ), self.now), date(2026, 10, 28))

        self.assertEqual(resolve_date(MonthDayDate(
            type='month_day', month=10, day=31, evidence='31 октября',
        ), self.now), date(2026, 10, 31))
        self.assertEqual(resolve_date(WeekdayDate(
            type='weekday', weekday=4, week_offset=1,
            evidence='в следующую пятницу',
        ), self.now), date(2026, 11, 6))
        self.assertEqual(resolve_date(PeriodDate(
            type='end_of_period', period='week', period_offset=0,
            evidence='в конце недели',
        ), self.now), date(2026, 11, 1))
        self.assertEqual(resolve_time(ParsedTime(
            hour=10, minute=0, evidence='в 10:00',
        )), time(10, 0))
        self.assertEqual(resolve_time(ParsedTime(
            type='absolute', hour=10, minute=0, evidence='10',
        )), time(10, 0))
        with self.assertRaises(DomainError):
            resolve_date(AbsoluteDate(
                type='absolute', year=2026, month=2, day=30,
            ), self.now)

    def test_malformed_relative_payload_is_normalized_from_evidence(self):
        self.assertEqual(normalize_date_payload({
            'type': 'relative', 'year': None,
            'resolution': 'nearest_future', 'evidence': 'на завтра',
        }), {
            'type': 'relative', 'days_offset': 1, 'evidence': 'на завтра',
        })

    def test_basic_relative_guard_overrides_wrong_model_type(self):
        guarded = normalize_date_payload({
            'type': 'weekday', 'weekday': 1, 'week_offset': 0,
            'evidence': 'завтра',
        })
        self.assertEqual(guarded, {
            'type': 'relative', 'days_offset': 1, 'evidence': 'завтра',
        })
        self.assertEqual(
            resolve_date(RelativeDate.model_validate(guarded), self.now),
            date(2026, 10, 28),
        )

    def test_weekday_evidence_overrides_incomplete_relative_payload(self):
        self.assertEqual(normalize_date_payload({
            'type': 'relative', 'evidence': 'В пятницу',
        }), {
            'type': 'weekday', 'weekday': 4, 'week_offset': 0,
            'evidence': 'В пятницу',
        })

    def test_relative_period_and_date_range_resolution(self):
        for offset, expected in (
            (0, date(2026, 10, 27)), (1, date(2026, 10, 28)),
            (2, date(2026, 10, 29)), (5, date(2026, 11, 1)),
        ):
            self.assertEqual(resolve_date(RelativeDate(
                type='relative', days_offset=offset,
            ), self.now), expected)
        current = resolve_temporal(PeriodExpression(
            type='period', unit='week', offset=0, mode='whole',
        ), self.now)
        self.assertEqual((current.start, current.end), (
            date(2026, 10, 26), date(2026, 11, 1),
        ))
        following = resolve_temporal(PeriodExpression(
            type='period', unit='week', offset=1, mode='whole',
        ), self.now)
        self.assertEqual((following.start, following.end), (
            date(2026, 11, 2), date(2026, 11, 8),
        ))
        until_end = resolve_temporal(PeriodExpression(
            type='period', unit='week', offset=0, mode='until_end',
        ), self.now)
        self.assertEqual((until_end.start, until_end.end), (
            date(2026, 10, 27), date(2026, 11, 1),
        ))
        november = resolve_temporal(DateRange(
            type='date_range',
            start={'type': 'month_day', 'month': 11, 'day': 8},
            end={'type': 'month_day', 'month': 11, 'day': 11},
        ), self.now)
        self.assertEqual((november.start, november.end), (
            date(2026, 11, 8), date(2026, 11, 11),
        ))
        new_year = resolve_temporal(DateRange(
            type='date_range',
            start={'type': 'month_day', 'month': 12, 'day': 28},
            end={'type': 'month_day', 'month': 1, 'day': 3},
        ), self.now)
        self.assertEqual((new_year.start, new_year.end), (
            date(2026, 12, 28), date(2027, 1, 3),
        ))

    def test_structured_month_day_skips_invalid_leap_year(self):
        end_of_year = Clock(fixed_now='2026-12-31T12:00:00+03:00').now()
        self.assertEqual(resolve_date(MonthDayDate(
            type='month_day', month=2, day=29,
        ), end_of_year), date(2028, 2, 29))

    def test_next_weekday_and_end_of_month(self):
        self.assertEqual(parse_day('в следующую пятницу', self.now), date(2026, 11, 6))
        self.assertEqual(parse_day('в конце месяца', self.now), date(2026, 10, 31))
        with self.assertRaisesRegex(DomainError, 'Какой день'):
            parse_day('на следующей неделе', self.now)

    def test_relative_hours(self):
        self.assertEqual(schedule('через два часа', '', self.now), (date(2026, 10, 27), time(14)))

    def test_half_hour_week_and_midnight_boundary(self):
        late = Clock(fixed_now='2026-12-31T23:45:00+03:00').now()
        self.assertEqual(schedule('через полчаса', '', late),
                         (date(2027, 1, 1), time(0, 15)))
        self.assertEqual(schedule('через неделю', '', late),
                         (date(2027, 1, 7), time(23, 45)))

    def test_day_without_year_moves_to_next_year_when_already_passed(self):
        end_of_year = Clock(fixed_now='2026-12-31T12:00:00+03:00').now()
        self.assertEqual(parse_day('1 января', end_of_year), date(2027, 1, 1))
        self.assertEqual(parse_day('31 декабря', end_of_year), date(2026, 12, 31))
        self.assertEqual(parse_day('29 февраля', end_of_year), date(2028, 2, 29))

    def test_week_boundaries(self):
        self.assertEqual(period_bounds('this_week', self.now), (date(2026, 10, 26), date(2026, 11, 1)))
        self.assertEqual(period_bounds('next_week', self.now), (date(2026, 11, 2), date(2026, 11, 8)))

    def test_time_is_optional_and_evening_supported(self):
        self.assertEqual(schedule('в пятницу', '', self.now), (date(2026, 10, 30), None))
        self.assertEqual(parse_time('в три дня'), time(15))
        self.assertEqual(parse_time('10 утра'), time(10))

    def test_invalid_dates_times_and_ambiguous_day(self):
        for value in ('31 февраля', 'на следующей неделе', 'какой-нибудь день'):
            with self.assertRaises(DomainError):
                parse_day(value, self.now)
        for value in ('25:70', 'вечером', 'утром', 'после обеда'):
            with self.assertRaises(DomainError):
                parse_time(value)

    def test_fixed_clock_is_converted_to_timezone(self):
        self.assertEqual(Clock(fixed_now='2026-10-27T23:30:00+00:00').now().date(), date(2026, 10, 28))

    def test_custom_period_requires_valid_bounds(self):
        with self.assertRaises(DomainError):
            period_bounds('custom', self.now, '2026-11-01', '2026-10-01')


class CsvTests(unittest.TestCase):
    def test_supplied_source_is_not_deduplicated_by_title(self):
        root = Path(os.getenv('DATA_DIR', str(Path(__file__).resolve().parents[2] / 'data')))
        path = root / 'voice_assistant_events_1000_77e927b5d2.csv'
        rows = read_csv(path)
        self.assertEqual(len(rows), 1000)
        self.assertEqual(len({raw['text'] for raw, _, _ in rows}), 376)
        self.assertEqual(rows[0][1], date(2024, 1, 7))


if __name__ == '__main__':
    unittest.main()
