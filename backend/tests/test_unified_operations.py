import os
import unittest
from datetime import date, time
from uuid import uuid4

from app.domain.operations import (
    Action, DurationInput, Interpretation, OperationIntent, RecordPatch,
    TargetQuery, TurnKind,
)
from app.domain.time import Clock, PeriodExpression
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
        if message == 'создай задачу до конца недели':
            return Interpretation(turn=TurnKind.NEW, intent=OperationIntent(
                action=Action.CREATE,
                changes=RecordPatch(
                    kind='task', text='Купить билеты',
                    temporal=PeriodExpression(
                        type='period', unit='week', offset=0,
                        mode='until_end', evidence='до конца недели',
                    ),
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
        if message == 'создай встречу с диапазоном':
            return Interpretation(turn=TurnKind.NEW, intent=OperationIntent(
                action=Action.CREATE,
                changes=RecordPatch(
                    kind='meeting', text='Встреча с мамой',
                    date_expression='завтра', time_expression='15:00',
                    duration=DurationInput(
                        type='range', min_minutes=60, max_minutes=120,
                        evidence='час-два',
                    ),
                ),
            ))
        if message == 'создай встречу без длительности':
            return Interpretation(turn=TurnKind.NEW, intent=OperationIntent(
                action=Action.CREATE,
                changes=RecordPatch(
                    kind='meeting', text='Встреча без точной длительности',
                    date_expression='завтра', time_expression='15:00',
                ),
            ))
        if message == 'создай раннюю встречу без длительности':
            return Interpretation(turn=TurnKind.NEW, intent=OperationIntent(
                action=Action.CREATE,
                changes=RecordPatch(
                    kind='meeting', text='Длинная встреча',
                    date_expression='завтра', time_expression='10:00',
                ),
            ))
        if message == 'создай блокирующую встречу':
            return Interpretation(turn=TurnKind.NEW, intent=OperationIntent(
                action=Action.CREATE,
                changes=RecordPatch(
                    kind='meeting', text='Встреча в середине',
                    date_expression='завтра', time_expression='11:30',
                    duration=DurationInput(
                        type='exact', minutes=30, evidence='30 минут',
                    ),
                ),
            ))
        if message == '2 часа':
            return Interpretation(
                turn=TurnKind.CONTINUE,
                intent=OperationIntent(changes=RecordPatch(
                    duration=DurationInput(
                        type='exact', minutes=120, evidence='2 часа',
                    ),
                )),
            )
        if message == 'не знаю':
            return Interpretation(
                turn=TurnKind.CONTINUE,
                intent=OperationIntent(unknown_fields=('duration',)),
            )
        if message == 'спроси длительность встречи с мамой':
            return Interpretation(turn=TurnKind.NEW, intent=OperationIntent(
                action=Action.READ,
                target=TargetQuery(
                    query='Встреча с мамой', kind='meeting', temporal='future',
                ),
                read_fields=('duration',),
            ))
        if message == 'покажи другое время':
            return Interpretation(
                turn=TurnKind.CONTINUE,
                intent=OperationIntent(alternative_requested=True),
            )
        if message == '18:00':
            return Interpretation(
                turn=TurnKind.CORRECTION,
                intent=OperationIntent(
                    changes=RecordPatch(time_expression='18:00'),
                    working_hours_override=True,
                ),
            )
        if message == 'создай созвон с альфой':
            return Interpretation(turn=TurnKind.NEW, intent=OperationIntent(
                action=Action.CREATE,
                changes=RecordPatch(
                    kind='meeting', text='Созвон с Альфой по презентации',
                    date_expression='2026-11-01', time_expression='15:00',
                    duration=DurationInput(minutes=75),
                ),
            ))
        if message == 'создай встречу с дашей':
            return Interpretation(turn=TurnKind.NEW, intent=OperationIntent(
                action=Action.CREATE,
                changes=RecordPatch(
                    kind='meeting', text='встреча с Дашей',
                    date_expression='2026-11-01', time_expression='15:00',
                    duration=DurationInput(minutes=120),
                ),
            ))
        if message == 'на следующий день':
            return Interpretation(
                turn=TurnKind.CORRECTION,
                intent=OperationIntent(changes=RecordPatch(
                    date_expression='2026-11-02', time_expression='08:00',
                )),
                parser_used='deterministic',
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
            con.execute('UPDATE sessions SET last_referenced_record_id=NULL WHERE id=%s',
                        (self.owner,))
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

        clarification = self.chat('перенеси и переименуй встречу')
        self.assertIn('после 18:00 или до 08:00', clarification['answer'])
        proposed = self.chat('18:00')
        self.assertTrue(proposed['pending'])
        self.assertIn('Вы действительно хотите изменить встречу', proposed['answer'])
        self.assertIn('Дата: 29.10.2026 → 30.10.2026', proposed['answer'])
        self.assertIn('Время: 11:00 → 18:00', proposed['answer'])
        self.assertIn('Длительность: 1 час → 1 ч 30 мин.', proposed['answer'])
        self.assertIn(
            'Название: Встреча с Марией → Переговоры с Марией',
            proposed['answer'],
        )
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

    def test_task_period_persists_start_and_end_dates(self):
        created = self.chat('создай задачу до конца недели')
        self.assertIsNone(created['pending'])
        self.assertEqual(created['records'][0]['scheduled_date'], '2026-10-28')
        self.assertEqual(created['records'][0]['scheduled_end_date'], '2026-11-01')
        row = self.rows()[0]
        self.assertEqual(row['scheduled_date'], date(2026, 10, 28))
        self.assertEqual(row['scheduled_end_date'], date(2026, 11, 1))

    def test_conflict_proposes_and_confirms_nearest_free_time(self):
        self.chat('создай первую встречу')
        proposed = self.chat('создай конфликтующую встречу')

        self.assertIn('Ближайшее свободное время', proposed['answer'])
        self.assertTrue(proposed['pending'])
        self.assertEqual(len(self.rows()), 1)

        completed = self.chat('подтверждаю')
        self.assertIsNone(completed['pending'])
        rows = {row['text']: row for row in self.rows()}
        self.assertEqual(rows['Встреча с Катей']['scheduled_time'], time(12))

    def test_overflow_asks_for_day_and_next_day_rechecks_from_eight(self):
        self.chat('создай созвон с альфой')
        waiting = self.chat('создай встречу с дашей')
        self.assertEqual(
            waiting['answer'],
            ('В этот день свободное время есть только после 16:15 '
             'или до 08:00. Когда вы хотите поставить встречу?'),
        )
        self.assertIsNotNone(waiting['pending'])
        completed = self.chat('на следующий день')
        self.assertIsNone(completed['pending'])
        dasha = next(row for row in self.rows() if row['text'] == 'встреча с Дашей')
        self.assertEqual(dasha['scheduled_date'], date(2026, 11, 2))
        self.assertEqual(dasha['scheduled_time'], time(8))

    def test_confirmation_state_accepts_alternative_request(self):
        self.chat('создай первую встречу')
        first = self.chat('создай конфликтующую встречу')
        self.assertIn('12:00', first['answer'])

        second = self.chat('покажи другое время')
        self.assertIn('12:30', second['answer'])
        self.assertEqual(len(self.rows()), 1)

        completed = self.chat('да')
        rows = {row['text']: row for row in self.rows()}
        self.assertEqual(rows['Встреча с Катей']['scheduled_time'], time(12, 30))

    def test_read_has_no_write_and_cancel_needs_confirmation(self):
        self.chat('создай первую встречу')
        self.chat('перенеси и переименуй встречу')
        self.chat('18:00')
        self.chat('да')
        version = self.rows()[0]['version']

        read = self.chat('покажи встречу')
        self.assertIn('Переговоры с Марией', read['answer'])
        self.assertEqual(self.rows()[0]['version'], version)

        proposed = self.chat('отмени встречу')
        self.assertTrue(proposed['pending'])
        self.assertEqual(len(self.rows()), 1)
        completed = self.chat('да')
        self.assertIn('Событие удалено', completed['answer'])
        self.assertEqual(self.rows(), [])

    def test_request_id_replay_does_not_repeat_create(self):
        request_id = uuid4()
        first = self.chat('создай первую встречу', request_id)
        second = self.chat('создай первую встречу', request_id)
        self.assertEqual(first, second)
        self.assertEqual(len(self.rows()), 1)

    def test_user_range_and_default_blocking_are_persisted_separately(self):
        ranged = self.chat('создай встречу с диапазоном')['records'][0]
        self.assertEqual(ranged['user_duration']['type'], 'range')
        self.assertEqual(ranged['user_duration']['min_minutes'], 60)
        self.assertEqual(ranged['user_duration']['max_minutes'], 120)
        self.assertEqual(ranged['blocking_duration_minutes'], 120)
        self.assertEqual(ranged['blocking_duration_source'], 'user_range_max')
        self.assertEqual(ranged['duration_status'], 'provided')

        answer = self.chat('спроси длительность встречи с мамой')
        self.assertIn('1–2 часа', answer['answer'])
        with self.repo.connect() as con:
            trace = con.execute(
                'SELECT raw_text,parsed_intent,search_query,candidate_ids '
                'FROM operation_search_traces WHERE owner_id=%s '
                'ORDER BY id DESC LIMIT 1', (self.owner,),
            ).fetchone()
        self.assertEqual(trace['search_query'], 'Встреча с мамой')
        self.assertEqual(trace['parsed_intent']['read_fields'], ['duration'])
        self.assertEqual(trace['candidate_ids'], [ranged['id']])

        owner = self.repo.new_session()
        try:
            service = Service(
                self.repo, assistant=object(), clock=self.clock,
                operation_interpreter=ScriptedInterpreter(),
            )
            waiting = service.chat(
                owner, uuid4(), 'создай встречу без длительности',
            )
            self.assertIsNotNone(waiting['pending'])
            self.assertIn('Сколько будет длиться', waiting['answer'])
            defaulted = service.chat(owner, uuid4(), 'не знаю')['records'][0]
            self.assertIsNone(defaulted['user_duration'])
            self.assertEqual(defaulted['blocking_duration_minutes'], 60)
            self.assertEqual(defaulted['duration_status'], 'declined_to_specify')
            self.assertEqual(
                defaulted['blocking_duration_source'], 'event_type_default',
            )
        finally:
            with self.repo.connect() as con:
                con.execute('UPDATE sessions SET last_referenced_record_id=NULL WHERE id=%s',
                            (owner,))
                for table in ('chat_requests', 'chat_messages', 'records'):
                    con.execute(f'DELETE FROM {table} WHERE owner_id=%s', (owner,))
                con.execute('DELETE FROM sessions WHERE id=%s', (owner,))

    def test_duration_answer_rechecks_conflicts_with_new_reserved_until(self):
        self.chat('создай блокирующую встречу')
        waiting = self.chat('создай раннюю встречу без длительности')
        self.assertIn('Сколько будет длиться', waiting['answer'])
        self.assertEqual(len(self.rows()), 1)

        conflict = self.chat('2 часа')
        self.assertIn('зарезервировано время до 12:00', conflict['answer'])
        self.assertIn('Ближайшее свободное время', conflict['answer'])
        self.assertEqual(len(self.rows()), 1)

        completed = self.chat('да')
        created = next(
            row for row in completed['records'] if row['text'] == 'Длинная встреча'
        )
        self.assertEqual(created['scheduled_time'], '12:00:00')
        self.assertEqual(created['blocking_duration_minutes'], 120)


if __name__ == '__main__':
    unittest.main()
