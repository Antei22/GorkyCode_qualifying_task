"""Regression checks for duration intent, provenance, and calendar scope."""
import os
import unittest
from datetime import date, time
from unittest.mock import Mock
from uuid import uuid4

from app.assistant.tools import Tools
from app.domain.duration import duration_request, explicit_duration
from app.domain.models import RecordDraft
from app.domain.time import Clock
from app.service import Service
from app.storage.repository import Repository


class DurationIntentTests(unittest.TestCase):
    def test_duration_accepts_number_before_or_after_unit(self):
        for message in ('3 часа', 'часа 3', 'три часа', 'часа три'):
            with self.subTest(message=message):
                self.assertEqual(explicit_duration(message), 180)

    def test_change_and_question_never_authorize_cancellation(self):
        repo, con = Mock(), Mock()
        for message in ('Исправь что встреча с марией будет длится долго',
                        'Сколько будет длиться встреча с Марией?',
                        'Не отменяй встречу с Марией'):
            with self.subTest(message=message):
                tools = Tools(repo, con, uuid4(), Clock(), None)
                tools.user_message = message
                result = tools.call('cancel_record', {'query': 'Мария'})
                self.assertIn('error', result)
        repo.cancel_record.assert_not_called()
        repo.search.assert_not_called()

    def test_future_past_and_explicit_date_are_preserved(self):
        future = duration_request('Сколько будет длится встреча с Марией?')
        self.assertEqual(future['temporal'], 'future')
        self.assertEqual(future['query'], 'встреча с марией')
        past = duration_request('Сколько длилась встреча с Марией 24 октября 2026?')
        self.assertEqual(past['temporal'], 'past')
        self.assertEqual(past['date_expression'], '24 октября 2026')
        self.assertEqual(past['query'], 'встреча с марией')


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL to a separate GorkyCode test DB')
class DurationStorageTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(os.environ['TEST_DATABASE_URL'])
        self.repo.initialize()
        self.owner = self.repo.new_session()
        self.clock = Clock(fixed_now='2026-10-28T12:00:00+03:00')
        self.assistant = Mock()
        self.assistant.answer.side_effect = AssertionError('This request must be handled on the server')
        self.service = Service(self.repo, self.assistant, self.clock)

    def tearDown(self):
        with self.repo.connect() as con:
            for table in ('chat_requests', 'chat_messages', 'records', 'sessions'):
                column = 'id' if table == 'sessions' else 'owner_id'
                con.execute(f'DELETE FROM {table} WHERE {column}=%s', (self.owner,))

    def seed(self, day=date(2026, 10, 29), minutes=60, source='estimated', text='Встреча с Марией'):
        with self.repo.connect() as con:
            return self.repo.create(con, self.owner, RecordDraft(
                kind='meeting', text=text, scheduled_date=day, scheduled_time=time(20),
                duration_minutes=minutes, duration_source=source, timezone='Europe/Moscow'))

    def chat(self, message):
        return self.service.chat(self.owner, uuid4(), message)

    def test_qualitative_update_keeps_same_record_active_and_unknown(self):
        row = self.seed()
        response = self.chat('Исправь что встреча с марией будет длится долго')
        self.assertIn('обновлена', response['answer'])
        with self.repo.connect() as con:
            rows = self.repo.list_records(con, self.owner, now=self.clock.now())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], row['id'])
        self.assertEqual(rows[0]['status'], 'open')
        self.assertEqual(rows[0]['duration_source'], 'estimated')
        self.assertEqual(rows[0]['duration_description'], 'долго')
        answer = self.chat('Сколько будет длиться встреча с Марией?')['answer']
        self.assertIn('Точная длительность не указана', answer)
        self.assertNotIn('120', answer)

    def test_future_question_excludes_history_and_import_tasks(self):
        future = self.seed()
        self.seed(day=date(2026, 10, 24), minutes=45, source='user')
        response = self.chat('Сколько будет длится встреча с Марией?')
        self.assertEqual([r['id'] for r in response['records']], [str(future['id'])])
        self.assertIn('Точная длительность не указана', response['answer'])

    def test_past_question_returns_known_plan_but_not_fabricated_actual_time(self):
        self.seed(day=date(2026, 10, 24), minutes=45, source='user')
        self.seed()
        response = self.chat('Сколько длилась встреча с Марией 24 октября 2026?')
        self.assertEqual(len(response['records']), 1)
        self.assertIn('45 мин', response['answer'])
        self.assertIn('Фактическая продолжительность не зафиксирована', response['answer'])

    def test_old_unknown_values_are_not_presented_as_user_supplied(self):
        self.seed(day=date(2026, 10, 24), minutes=90, source='unknown')
        response = self.chat('Сколько длилась встреча с Марией?')
        self.assertIn('Точная длительность не указана', response['answer'])
        self.assertNotIn('90 мин', response['answer'])

    def test_explicit_update_and_followup_modify_in_place(self):
        row = self.seed()
        response = self.chat('Исправь длительность встречи с Марией')
        self.assertIsNotNone(response['pending'])
        response = self.chat('45 минут')
        self.assertIsNone(response['pending'])
        self.assertEqual(response['records'][0]['id'], str(row['id']))
        self.assertEqual(response['records'][0]['duration_source'], 'user')
        self.assertIn('45 мин', self.chat('Сколько будет длиться встреча с Марией?')['answer'])

    def test_unknown_creation_is_estimated(self):
        with self.repo.connect() as con:
            tools = Tools(self.repo, con, self.owner, self.clock, None)
            tools.user_message = 'Создай встречу с Марией завтра в 20'
            tools.call('create_record', {'kind': 'meeting', 'text': 'Встреча с Марией',
                'date_expression': 'завтра', 'time_expression': '20:00'})
            self.assertTrue(tools.pending)
        response = self.chat('Не знаю точно, достаточно долго')
        self.assertEqual(response['records'][0]['duration_source'], 'estimated')
        self.assertEqual(response['records'][0]['duration_minutes'], 120)

    def test_slot_confirmation_does_not_turn_estimate_into_exact_duration(self):
        self.seed(day=date(2026, 10, 29), text='Встреча с дизайнером')
        with self.repo.connect() as con:
            tools = Tools(self.repo, con, self.owner, self.clock, None)
            tools.user_message = 'Не знаю'
            tools.call('create_record', {'kind': 'meeting', 'text': 'Встреча с Марией',
                'date_expression': 'завтра', 'time_expression': '20:00',
                'duration_unspecified': True})
            self.assertIn('_suggested_slot', tools.pending)
        response = self.chat('да')
        self.assertEqual(response['records'][0]['duration_source'], 'estimated')

    def test_imported_meeting_without_duration_is_readable_but_related_tasks_are_excluded(self):
        with self.repo.connect() as con:
            for title in ('Встреча с Марией по аналитике', 'Отправить итоги встречи Марией'):
                con.execute('''INSERT INTO records
                    (id,owner_id,text,scheduled_date,scheduled_time,timezone,source)
                    VALUES (%s,%s,%s,%s,%s,'Europe/Moscow','csv')''',
                    (uuid4(), self.owner, title, date(2026, 10, 24), time(9)))
        response = self.chat('Сколько длилась встреча с Марией 24 октября 2026?')
        self.assertEqual(len(response['records']), 1)
        self.assertIsNone(response['records'][0]['kind'])
        self.assertIn('Точная длительность не указана', response['answer'])

    def test_ambiguous_update_does_not_change_any_record(self):
        self.seed()
        self.seed(day=date(2026, 10, 30))
        response = self.chat('Исправь что встреча с Марией будет длиться долго')
        self.assertIn('несколько', response['answer'])
        with self.repo.connect() as con:
            rows = self.repo.list_records(con, self.owner, now=self.clock.now())
        self.assertTrue(all(r['duration_minutes'] == 60 and r['status'] == 'open' for r in rows))

    def test_date_selection_preserves_requested_change(self):
        selected = self.seed()
        self.seed(day=date(2026, 10, 30))
        self.chat('Исправь длительность встречи с Марией на 45 минут')

        def select_date(message, history, tools):
            tools.call('update_meeting_duration', {
                'mode': 'continue', 'date_expression': '29 октября 2026'})
            return ''

        self.assistant.answer.side_effect = select_date
        response = self.chat('29 октября 2026')
        self.assertEqual(response['records'][0]['id'], str(selected['id']))
        self.assertEqual(response['records'][0]['duration_minutes'], 45)
        self.assertEqual(response['records'][0]['duration_source'], 'user')
        self.assertIsNone(response['pending'])
