import unittest
import os
from datetime import date, time
from pathlib import Path
from app.domain.models import DomainError
from app.domain.time import Clock, parse_day, parse_time, period_bounds, schedule
from app.ingestion.csv_import import read_csv


class TimeTests(unittest.TestCase):
    def setUp(self):
        self.now = Clock(fixed_now='2026-10-27T12:00:00+03:00').now()

    def test_relative_dates_and_weekday(self):
        self.assertEqual(parse_day('завтра', self.now), date(2026, 10, 28))
        self.assertEqual(parse_day('послезавтра', self.now), date(2026, 10, 29))
        self.assertEqual(parse_day('в пятницу', self.now), date(2026, 10, 30))
        self.assertEqual(parse_day('до конца недели', self.now), date(2026, 11, 1))

    def test_relative_hours(self):
        self.assertEqual(schedule('через два часа', '', self.now), (date(2026, 10, 27), time(14)))

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
        with self.assertRaises(DomainError):
            parse_time('25:70')

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
