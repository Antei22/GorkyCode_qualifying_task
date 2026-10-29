import os
import unittest
from datetime import date, time
from uuid import uuid4

from app.domain.operations import (
    Action, DurationInput, Interpretation, OperationIntent, RecordPatch,
    TargetQuery, TurnKind,
)
from app.domain.time import Clock
from app.service import Service
from app.storage.repository import Repository


class ScriptedInterpreter:
    def interpret(self, context, pending):
        message = context.message.lower()
        if message in {'да', 'подтверждаю'}:
            return Interpretation(turn=TurnKind.CONFIRM)
        if message == 'создай первую встречу':
            return Interpretation(turn=TurnKind.NEW, intent=OperationIntent(
                action=Action.CREATE,
                changes=RecordPatch(
                    kind='meeting', text='Встреча с Марией',
                    date_expression='завтра', time_expression='11:00',
                    duration=DurationInput(minutes=60),
                ),
            ))
        if message == 'создай конфликтующую встречу':
            return Interpretation(turn=TurnKind.NEW, intent=OperationIntent(
                action=Action.CREATE,
                changes=RecordPatch(
                    kind='meeting', text='Встреча с Катей',
                    date_expression='завтра', time_expression='11:20',
                    duration=DurationInput(minutes=30),
                ),
            ))
        if message == 'покажи другое время':
            return Interpretation(
                turn=TurnKind.CONTINUE,
                intent=OperationIntent(alternative_requested=True),
            )
        if message == 'только рабочее время':
            return Interpretation(
                turn=TurnKind.CONTINUE,
                intent=OperationIntent(calendar_scope='work_hours'),
            )
        if message == 'перенеси и переименуй встречу':
            return Interpretation(turn=TurnKind.NEW, intent=OperationIntent(
                action=Action.UPDATE,
                target=TargetQuery(query='Встреча с Марией', temporal='future'),
                changes=RecordPatch(
                    text='Переговоры с Марией', date_expression='послезавтра',
                    time_expression='18:00', duration=DurationInput(minutes=90),
                ),
            ))
        if message == 'покажи встречу':
            return Interpretation(turn=TurnKind.NEW, intent=OperationIntent(
                action=Action.READ,
                target=TargetQuery(query='Мария', temporal='all'),
            ))
        if message == 'отмени встречу':
            return Interpretation(turn=TurnKind.NEW, intent=OperationIntent(
                action=Action.CANCEL,
                target=TargetQuery(query='Переговоры с Марией', temporal='future'),
            ))
        raise AssertionError(f'unexpected message: {context.message}')


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'),
                     'Set TEST_DATABASE_URL to isolated GorkyCode DB')
class UnifiedPostgresTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(os.environ['TEST_DATABASE_URL'])
        self.repo.initialize()
        self.owner = self.repo.new_session()
        self.clock = Clock(fixed_now='2026-10-28T12:00:00+03:00')
        self.service = Service(
            self.repo, assistant=object(), clock=self.clock,
            operation_interpreter=ScriptedInterpreter(),
        )

    def tearDown(self):
        with self.repo.connect() as con:
            for table in ('chat_requests', 'chat_messages', 'records'):
                con.execute(f'DELETE FROM {table} WHERE owner_id=%s', (self.owner,))
            con.execute('DELETE FROM sessions WHERE id=%s', (self.owner,))

    def chat(self, message, request_id=None):
        return self.service.chat(self.owner, request_id or uuid4(), message)

    def rows(self):
        with self.repo.connect() as con:
            return self.repo.list_records(con, self.owner, now=self.clock.now())

    def test_multi_field_update_preserves_identity_and_requires_confirmation(self):
        created = self.chat('создай первую встречу')
        record_id = created['records'][0]['id']

        proposed = self.chat('перенеси и переименуй встречу')
        self.assertTrue(proposed['pending'])
        self.assertIn('Подтвердите', proposed['answer'])
        before = self.rows()[0]
        self.assertEqual(str(before['scheduled_date']), '2026-10-29')

        completed = self.chat('да')
        self.assertIsNone(completed['pending'])
        self.assertEqual(completed['records'][0]['id'], record_id)
        row = self.rows()[0]
        self.assertEqual(str(row['id']), record_id)
        self.assertEqual(row['text'], 'Переговоры с Марией')
        self.assertEqual(row['scheduled_date'], date(2026, 10, 30))
        self.assertEqual(row['scheduled_time'], time(18))
        self.assertEqual(row['duration_minutes'], 90)
        self.assertEqual(row['version'], 2)

    def test_conflict_proposes_and_confirms_nearest_free_time(self):
        self.chat('создай первую встречу')
        scope = self.chat('создай конфликтующую встречу')
        self.assertIn('только по будням', scope['answer'])

        proposed = self.chat('только рабочее время')
        self.assertIn('Ближайшее свободное время', proposed['answer'])
        self.assertTrue(proposed['pending'])
        self.assertEqual(len(self.rows()), 1)

        completed = self.chat('подтверждаю')
        self.assertIsNone(completed['pending'])
        rows = {row['text']: row for row in self.rows()}
        self.assertEqual(rows['Встреча с Катей']['scheduled_time'], time(8))

    def test_conflict_can_offer_the_next_alternative(self):
        self.chat('создай первую встречу')
        self.chat('создай конфликтующую встречу')
        first = self.chat('только рабочее время')
        self.assertIn('08:00', first['answer'])

        second = self.chat('покажи другое время')
        self.assertIn('Следующее свободное время', second['answer'])
        self.assertIn('08:30', second['answer'])
        self.assertEqual(len(self.rows()), 1)

        completed = self.chat('да')
        rows = {row['text']: row for row in self.rows()}
        self.assertEqual(rows['Встреча с Катей']['scheduled_time'], time(8, 30))

    def test_read_has_no_write_and_cancel_needs_confirmation(self):
        self.chat('создай первую встречу')
        self.chat('перенеси и переименуй встречу')
        self.chat('да')
        version = self.rows()[0]['version']

        read = self.chat('покажи встречу')
        self.assertIn('Переговоры с Марией', read['answer'])
        self.assertEqual(self.rows()[0]['version'], version)

        proposed = self.chat('отмени встречу')
        self.assertTrue(proposed['pending'])
        self.assertEqual(len(self.rows()), 1)
        completed = self.chat('да')
        self.assertIn('отменена', completed['answer'])
        self.assertEqual(self.rows(), [])

    def test_request_id_replay_does_not_repeat_create(self):
        request_id = uuid4()
        first = self.chat('создай первую встречу', request_id)
        second = self.chat('создай первую встречу', request_id)
        self.assertEqual(first, second)
        self.assertEqual(len(self.rows()), 1)


if __name__ == '__main__':
    unittest.main()
