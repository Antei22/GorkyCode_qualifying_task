import unittest
from datetime import date, datetime, time
from uuid import uuid4

from app.application.operations.calendar_policy import CalendarOperationPolicy
from app.application.operations.ports import TurnContext
from app.domain.operations import (
    Action, DurationValue, OperationIntent, Phase, RecordPatch, RecordSnapshot,
    make_change_plan,
)


class MeetingRepository:
    def __init__(self, rows):
        self.rows = rows

    def meetings_between(self, connection, owner, first, last):
        return [row for row in self.rows
                if first <= row['scheduled_date'] <= last]


def context():
    return TurnContext(
        owner_id=uuid4(), request_id=uuid4(), message='test',
        now=datetime.fromisoformat('2026-10-29T12:00:00+03:00'),
    )


def intent(scope=None, at='15:00', duration=240):
    return OperationIntent(
        action=Action.CREATE,
        changes=RecordPatch(
            kind='meeting', text='Встреча с дизайнером',
            date_expression='2026-10-30', time_expression=at,
            duration={'minutes': duration},
        ),
        calendar_scope=scope,
    )


def plan(at=time(15), duration=240):
    return make_change_plan(
        'create', None,
        RecordSnapshot(
            id=uuid4(), kind='meeting', text='Встреча с дизайнером',
            scheduled_date=date(2026, 10, 30), scheduled_time=at,
            duration=DurationValue(minutes=duration, source='user'),
            timezone='Europe/Moscow', status='open', revision='new',
        ),
    )


def conflict(duration=120, at=time(15)):
    return {
        'id': uuid4(), 'text': 'Занятый слот',
        'scheduled_date': date(2026, 10, 30),
        'scheduled_time': at, 'duration_minutes': duration,
    }


class CalendarScopeTests(unittest.TestCase):
    def policy(self, rows):
        return CalendarOperationPolicy(MeetingRepository(rows), object())

    def test_conflict_first_asks_for_calendar_scope(self):
        decision = self.policy([conflict()]).evaluate(
            context(), intent(), plan(), None,
        )

        self.assertEqual(decision.phase, Phase.NEED_FIELDS)
        self.assertEqual(decision.missing_fields, ('calendar_scope',))
        self.assertIn('только по будням', decision.question)
        self.assertIn('только по выходным', decision.question)
        self.assertIn('с 08:00 до 22:00', decision.question)
        self.assertIn(
            '«Занятый слот», которая длится 30.10.2026 с 15:00 до 17:00',
            decision.question,
        )

    def test_conflict_message_shows_existing_interval_not_requested_start(self):
        decision = self.policy([conflict(duration=240)]).evaluate(
            context(), intent(at='18:00'), plan(at=time(18)), None,
        )

        self.assertIn('30.10.2026 с 15:00 до 19:00', decision.question)
        self.assertNotIn(
            'На 30.10.2026 в 18:00 уже запланирована', decision.question,
        )

    def test_work_hours_skip_weekend_and_offer_monday(self):
        decision = self.policy([conflict(duration=840, at=time(8))]).evaluate(
            context(), intent('work_hours'), plan(), None,
        )

        self.assertEqual(decision.phase, Phase.NEED_CONFIRMATION)
        self.assertEqual(decision.alternative_plan.after.scheduled_date,
                         date(2026, 11, 2))
        self.assertEqual(decision.alternative_plan.after.scheduled_time, time(8))

    def test_extended_hours_can_offer_saturday(self):
        decision = self.policy([conflict(duration=840, at=time(8))]).evaluate(
            context(), intent('extended_hours'), plan(), None,
        )

        self.assertEqual(decision.phase, Phase.NEED_CONFIRMATION)
        self.assertEqual(decision.alternative_plan.after.scheduled_date,
                         date(2026, 10, 31))
        self.assertEqual(decision.alternative_plan.after.scheduled_time, time(8))

    def test_weekends_only_skips_friday_and_offers_saturday(self):
        decision = self.policy([conflict()]).evaluate(
            context(), intent('weekends_extended_hours'), plan(), None,
        )

        self.assertEqual(decision.phase, Phase.NEED_CONFIRMATION)
        self.assertEqual(decision.alternative_plan.after.scheduled_date,
                         date(2026, 10, 31))
        self.assertEqual(decision.alternative_plan.after.scheduled_time, time(8))

    def test_weekday_any_time_searches_from_opening_not_conflicting_time(self):
        decision = self.policy([conflict(duration=120, at=time(15, 30))]).evaluate(
            context(),
            intent('weekdays_extended_hours', at='13:00', duration=180),
            plan(at=time(13), duration=180), None,
        )

        self.assertEqual(decision.phase, Phase.NEED_CONFIRMATION)
        self.assertEqual(decision.alternative_plan.after.scheduled_date,
                         date(2026, 10, 30))
        self.assertEqual(decision.alternative_plan.after.scheduled_time, time(8))

    def test_next_option_starts_after_previous_proposal_ends(self):
        result = self.policy([])._next_free(
            context().owner_id, datetime(2026, 10, 30, 8), 180, None,
            'weekdays_extended_hours',
        )

        self.assertEqual(result, datetime(2026, 10, 30, 11))


if __name__ == '__main__':
    unittest.main()
