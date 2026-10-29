"""Run only against GorkyCode's own DB: TEST_DATABASE_URL must be explicit.

Each test uses fresh session UUIDs and removes only rows belonging to those UUIDs.
"""
import os
import threading
import unittest
from pathlib import Path
from uuid import uuid4
from fastapi.testclient import TestClient
from app.config import Settings
from app.domain.models import DomainError
from app.domain.time import Clock
from app.ingestion.csv_import import import_csv
from app.main import create_app
from app.service import Service
from app.storage.repository import Repository


class ScriptedAssistant:
    def answer(self, message, history, tools):
        if message == 'в 11':
            tools.call('create_record', {'mode': 'continue', 'time_expression': '11:00'})
        elif message == 'без времени':
            tools.call('create_record', {'mode': 'start', 'kind': 'meeting', 'text': 'Иван',
                                         'date_expression': 'завтра', 'duration_minutes': 60})
        else:
            tools.call('create_record', {'mode': 'start', 'kind': 'task', 'text': message,
                                         'date_expression': 'в пятницу'})
        return 'Ответ модели'


class MultiStepAssistant:
    calls = {
        'создай напоминание': {'mode': 'start', 'kind': 'reminder'},
        'позвонить маме': {'mode': 'continue', 'text': 'Позвонить маме'},
        'завтра': {'mode': 'continue', 'date_expression': 'завтра'},
        'в 18': {'mode': 'continue', 'time_expression': '18:00'},
        'неверная дата': {'mode': 'continue', 'date_expression': 'когда-нибудь'},
        'новая задача': {'mode': 'start', 'kind': 'task', 'text': 'Новая задача'},
        'продолжить без черновика': {'mode': 'continue', 'time_expression': '10:00'},
        'задача со временем': {'mode': 'start', 'kind': 'task', 'text': 'Позвонить',
                              'time_expression': '10:00'},
        'запиши созвон': {'mode': 'start', 'text': 'Созвон с Иваном'},
        'это встреча': {'mode': 'continue', 'kind': 'meeting'},
        'позвонить Ивану завтра в 10': {
            'mode': 'continue', 'text': 'Позвонить Ивану',
            'date_expression': 'завтра', 'time_expression': '10:00',
        },
        'позвонить Ивану завтра': {
            'mode': 'continue', 'text': 'Позвонить Ивану',
            'date_expression': 'завтра',
        },
        'напомни сегодня в 10': {
            'mode': 'start', 'kind': 'reminder', 'text': 'Позвонить Ивану',
            'date_expression': 'сегодня', 'time_expression': '10:00',
        },
        'напомни вчера': {
            'mode': 'start', 'kind': 'reminder', 'text': 'Позвонить Ивану',
            'date_expression': 'вчера', 'time_expression': '10:00',
        },
        'купить билеты': {'mode': 'start', 'kind': 'task', 'text': 'Купить билеты'},
        'отправить отчёт в пятницу': {
            'mode': 'start', 'kind': 'task', 'text': 'Отправить отчёт',
            'date_expression': 'в пятницу',
        },
        'встреча с дизайнером': {
            'mode': 'start', 'kind': 'meeting', 'text': 'Встреча с дизайнером',
            'date_expression': 'завтра', 'time_expression': '11:00', 'duration_minutes': 60,
        },
        'созвониться с Катей': {
            'mode': 'start', 'kind': 'meeting', 'text': 'Созвониться с Катей',
            'date_expression': 'завтра', 'time_expression': '11:00', 'duration_minutes': 30,
        },
        'час': {'mode': 'continue', 'duration_minutes': 60},
        'в 12': {'mode': 'continue', 'time_expression': '12:00'},
    }

    def answer(self, message, history, tools):
        tools.call('create_record', self.calls[message])
        return tools.questions[-1] if tools.questions else 'Ответ модели'


@unittest.skipUnless(os.getenv('TEST_DATABASE_URL'), 'Set TEST_DATABASE_URL to isolated GorkyCode DB')
class PostgresTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repository(os.environ['TEST_DATABASE_URL'])
        self.repo.initialize()
        self.owners = [self.repo.new_session(), self.repo.new_session()]
        self.clock = Clock(fixed_now='2026-10-27T12:00:00+03:00')
        self.service = Service(self.repo, ScriptedAssistant(), self.clock)

    def tearDown(self):
        with self.repo.connect() as con:
            for owner in self.owners:
                for table in ('chat_requests', 'chat_messages', 'records', 'sessions'):
                    column = 'id' if table == 'sessions' else 'owner_id'
                    con.execute(f'DELETE FROM {table} WHERE {column}=%s', (owner,))

    def rows(self, owner=None):
        with self.repo.connect() as con:
            return self.repo.list_records(con, owner or self.owners[0])

    def test_idempotent_creation_and_conflicting_request(self):
        request = uuid4()
        first = self.service.chat(self.owners[0], request, 'Проверить договор')
        self.assertEqual(first, self.service.chat(self.owners[0], request, 'Проверить договор'))
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNone(self.rows()[0]['scheduled_time'])
        with self.assertRaises(DomainError):
            self.service.chat(self.owners[0], request, 'Другой текст')

    def test_clarification_survives_service_restart(self):
        result = self.service.chat(self.owners[0], uuid4(), 'без времени')
        self.assertTrue(result['pending'])
        self.assertEqual(len(self.rows()), 0)
        service = Service(self.repo, ScriptedAssistant(), self.clock)
        service.chat(self.owners[0], uuid4(), 'в 11')
        self.assertEqual(str(self.rows()[0]['scheduled_time']), '11:00:00')
        self.assertEqual(len(self.rows()), 1)

    def test_reminder_is_collected_one_field_at_a_time(self):
        service = Service(self.repo, MultiStepAssistant(), self.clock)
        steps = [
            ('создай напоминание', 'Что должно быть записано?'),
            ('позвонить маме', 'На какую дату?'),
            ('завтра', 'На какое время?'),
        ]
        for message, expected in steps:
            result = service.chat(self.owners[0], uuid4(), message)
            self.assertEqual(result['answer'], expected)
            self.assertTrue(result['pending'])
            self.assertIn('created_at', result['pending'])
            self.assertIn('updated_at', result['pending'])
            self.assertEqual(len(self.rows()), 0)

        result = service.chat(self.owners[0], uuid4(), 'в 18')
        self.assertIn('Напоминание сохранено', result['answer'])
        self.assertIsNone(result['pending'])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(str(self.rows()[0]['scheduled_time']), '18:00:00')

    def test_missing_type_is_requested_before_schedule(self):
        service = Service(self.repo, MultiStepAssistant(), self.clock)

        first = service.chat(self.owners[0], uuid4(), 'запиши созвон')
        second = service.chat(self.owners[0], uuid4(), 'это встреча')
        third = service.chat(self.owners[0], uuid4(), 'завтра')
        result = service.chat(self.owners[0], uuid4(), 'в 18')

        self.assertEqual(first['answer'], 'Что создать: задачу, встречу или напоминание?')
        self.assertEqual(second['answer'], 'На какую дату?')
        self.assertEqual(third['answer'], 'На какое время?')
        self.assertEqual(result['answer'], 'Сколько будет длиться встреча?')
        self.assertEqual(len(self.rows()), 0)
        result = service.chat(self.owners[0], uuid4(), 'час')
        self.assertIn('Встреча сохранена', result['answer'])
        self.assertEqual(self.rows()[0]['text'], 'Созвон с Иваном')

    def test_combined_clarification_answer_can_complete_reminder(self):
        service = Service(self.repo, MultiStepAssistant(), self.clock)
        service.chat(self.owners[0], uuid4(), 'создай напоминание')

        result = service.chat(self.owners[0], uuid4(), 'позвонить Ивану завтра в 10')

        self.assertIn('Напоминание сохранено', result['answer'])
        self.assertIsNone(result['pending'])
        row = self.rows()[0]
        self.assertEqual(row['text'], 'Позвонить Ивану')
        self.assertEqual(str(row['scheduled_date']), '2026-10-28')
        self.assertEqual(str(row['scheduled_time']), '10:00:00')

    def test_partial_combined_answer_only_asks_for_time(self):
        service = Service(self.repo, MultiStepAssistant(), self.clock)
        service.chat(self.owners[0], uuid4(), 'создай напоминание')

        result = service.chat(self.owners[0], uuid4(), 'позвонить Ивану завтра')

        self.assertEqual(result['answer'], 'На какое время?')
        self.assertEqual(result['pending']['text'], 'Позвонить Ивану')
        self.assertEqual(result['pending']['date_expression'], 'завтра')
        self.assertNotIn('time_expression', result['pending'])
        self.assertEqual(len(self.rows()), 0)

    def test_task_type_rules_allow_no_deadline_or_date_without_time(self):
        service = Service(self.repo, MultiStepAssistant(), self.clock)

        first = service.chat(self.owners[0], uuid4(), 'купить билеты')
        second = service.chat(self.owners[0], uuid4(), 'отправить отчёт в пятницу')

        self.assertIn('Задача сохранена', first['answer'])
        self.assertIn('Задача сохранена', second['answer'])
        rows = {row['text']: row for row in self.rows()}
        self.assertIsNone(rows['Купить билеты']['scheduled_date'])
        self.assertIsNone(rows['Купить билеты']['scheduled_time'])
        self.assertEqual(str(rows['Отправить отчёт']['scheduled_date']), '2026-10-30')
        self.assertIsNone(rows['Отправить отчёт']['scheduled_time'])

    def test_past_date_and_time_are_rejected_without_losing_other_fields(self):
        service = Service(self.repo, MultiStepAssistant(), self.clock)

        past_time = service.chat(self.owners[0], uuid4(), 'напомни сегодня в 10')
        self.assertEqual(past_time['answer'],
                         'Указанное время уже прошло. Выберите другое время.')
        self.assertEqual(past_time['pending']['text'], 'Позвонить Ивану')
        self.assertEqual(past_time['pending']['date_expression'], 'сегодня')
        self.assertNotIn('time_expression', past_time['pending'])

        past_date = service.chat(self.owners[0], uuid4(), 'напомни вчера')
        self.assertEqual(past_date['answer'],
                         'Указанная дата уже прошла. Выберите другую дату.')
        self.assertEqual(past_date['pending']['text'], 'Позвонить Ивану')
        self.assertNotIn('date_expression', past_date['pending'])
        self.assertEqual(len(self.rows()), 0)

    def test_meeting_conflict_keeps_draft_and_accepts_another_time(self):
        service = Service(self.repo, MultiStepAssistant(), self.clock)
        first = service.chat(self.owners[0], uuid4(), 'встреча с дизайнером')
        self.assertIn('Встреча сохранена', first['answer'])

        conflict = service.chat(self.owners[0], uuid4(), 'созвониться с Катей')

        self.assertEqual(
            conflict['answer'],
            'На 28.10.2026 в 11:00 уже запланирована встреча '
            '«Встреча с дизайнером». Ближайшее свободное время — 28.10.2026 в 12:00. Подтвердить?',
        )
        self.assertEqual(conflict['pending']['text'], 'Созвониться с Катей')
        self.assertEqual(conflict['pending']['time_expression'], '11:00')
        self.assertEqual(len(self.rows()), 1)

        moved = service.chat(self.owners[0], uuid4(), 'в 12')

        self.assertIn('Встреча сохранена', moved['answer'])
        self.assertIsNone(moved['pending'])
        rows = {row['text']: row for row in self.rows()}
        self.assertEqual(str(rows['Встреча с дизайнером']['scheduled_time']), '11:00:00')
        self.assertEqual(str(rows['Созвониться с Катей']['scheduled_time']), '12:00:00')

    def test_new_creation_does_not_merge_with_pending_draft(self):
        service = Service(self.repo, MultiStepAssistant(), self.clock)
        service.chat(self.owners[0], uuid4(), 'создай напоминание')
        service.chat(self.owners[0], uuid4(), 'позвонить маме')
        service.chat(self.owners[0], uuid4(), 'завтра')

        result = service.chat(self.owners[0], uuid4(), 'новая задача')

        self.assertIn('Задача сохранена', result['answer'])
        self.assertIsNone(result['pending'])
        row = self.rows()[0]
        self.assertEqual(row['text'], 'Новая задача')
        self.assertIsNone(row['scheduled_date'])
        self.assertIsNone(row['scheduled_time'])

    def test_invalid_date_keeps_other_draft_fields(self):
        service = Service(self.repo, MultiStepAssistant(), self.clock)
        service.chat(self.owners[0], uuid4(), 'создай напоминание')
        service.chat(self.owners[0], uuid4(), 'позвонить маме')

        invalid = service.chat(self.owners[0], uuid4(), 'неверная дата')

        self.assertIn('Уточните дату', invalid['answer'])
        self.assertEqual(invalid['pending']['kind'], 'reminder')
        self.assertEqual(invalid['pending']['text'], 'Позвонить маме')
        self.assertNotIn('date_expression', invalid['pending'])
        self.assertEqual(len(self.rows()), 0)

        corrected = service.chat(self.owners[0], uuid4(), 'завтра')
        self.assertEqual(corrected['answer'], 'На какое время?')
        created = service.chat(self.owners[0], uuid4(), 'в 18')
        self.assertIn('Напоминание сохранено', created['answer'])

    def test_continue_without_draft_is_rejected(self):
        result = Service(self.repo, MultiStepAssistant(), self.clock).chat(
            self.owners[0], uuid4(), 'продолжить без черновика')
        self.assertEqual(result['answer'],
                         'Нет незавершённого черновика. Начните создание заново.')
        self.assertEqual(len(self.rows()), 0)

    def test_task_with_time_requires_date(self):
        result = Service(self.repo, MultiStepAssistant(), self.clock).chat(
            self.owners[0], uuid4(), 'задача со временем')
        self.assertEqual(result['answer'], 'На какую дату?')
        self.assertTrue(result['pending'])
        self.assertEqual(len(self.rows()), 0)

    def test_expired_draft_is_cleared(self):
        service = Service(self.repo, MultiStepAssistant(), self.clock)
        service.chat(self.owners[0], uuid4(), 'создай напоминание')
        self.clock.fixed_now = '2026-10-27T12:31:00+03:00'

        result = service.chat(self.owners[0], uuid4(), 'позвонить маме')

        self.assertEqual(result['answer'], 'Черновик устарел. Начните создание заново.')
        self.assertIsNone(result['pending'])
        self.assertEqual(len(self.rows()), 0)

    def test_session_isolation_and_russian_search(self):
        self.service.chat(self.owners[0], uuid4(), 'Проверить договор')
        self.assertEqual(len(self.rows(self.owners[1])), 0)
        with self.repo.connect() as con:
            self.assertEqual(len(self.repo.search(con, self.owners[0], 'договора')), 1)
            self.assertEqual(len(self.repo.search(con, self.owners[1], 'договора')), 0)

    def test_model_failure_rolls_back_creation(self):
        class FailedAssistant(ScriptedAssistant):
            def answer(self, message, history, tools):
                super().answer(message, history, tools)
                raise DomainError('Model failed after tool call')
        with self.assertRaises(DomainError):
            Service(self.repo, FailedAssistant(), self.clock).chat(self.owners[0], uuid4(), 'Не сохранять')
        self.assertEqual(len(self.rows()), 0)
        with self.repo.connect() as con:
            session = con.execute('SELECT pending FROM sessions WHERE id=%s',
                                  (self.owners[0],)).fetchone()
            messages = con.execute('SELECT count(*) AS n FROM chat_messages WHERE owner_id=%s',
                                   (self.owners[0],)).fetchone()['n']
            requests = con.execute('SELECT count(*) AS n FROM chat_requests WHERE owner_id=%s',
                                   (self.owners[0],)).fetchone()['n']
        self.assertIsNone(session['pending'])
        self.assertEqual(messages, 0)
        self.assertEqual(requests, 0)

    def test_model_failure_rolls_back_pending_draft(self):
        class FailedDraftAssistant:
            def answer(self, message, history, tools):
                tools.call('create_record', {'mode': 'start', 'kind': 'meeting',
                                             'text': 'Созвон с Иваном'})
                raise DomainError('Model failed after draft update')

        with self.assertRaises(DomainError):
            Service(self.repo, FailedDraftAssistant(), self.clock).chat(
                self.owners[0], uuid4(), 'Создай встречу')

        with self.repo.connect() as con:
            session = con.execute('SELECT pending FROM sessions WHERE id=%s',
                                  (self.owners[0],)).fetchone()
            messages = con.execute('SELECT count(*) AS n FROM chat_messages WHERE owner_id=%s',
                                   (self.owners[0],)).fetchone()['n']
            requests = con.execute('SELECT count(*) AS n FROM chat_requests WHERE owner_id=%s',
                                   (self.owners[0],)).fetchone()['n']
        self.assertIsNone(session['pending'])
        self.assertEqual(messages, 0)
        self.assertEqual(requests, 0)
        self.assertEqual(len(self.rows()), 0)

    def test_concurrent_turns_of_one_session_are_serialized(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        class ConcurrentAssistant:
            def answer(self, message, history, tools):
                if message == 'start':
                    first_entered.set()
                    if not release_first.wait(timeout=5):
                        raise DomainError('Test timeout while holding session lock')
                    tools.call('create_record', {'mode': 'start', 'kind': 'reminder'})
                else:
                    second_entered.set()
                    tools.call('create_record', {'mode': 'continue',
                                                 'text': 'Позвонить Ивану'})
                return tools.questions[-1] if tools.questions else 'Ответ модели'

        service = Service(self.repo, ConcurrentAssistant(), self.clock)
        responses = {}
        errors = []

        def run_turn(name, message):
            try:
                responses[name] = service.chat(self.owners[0], uuid4(), message)
            except Exception as error:  # failures are asserted in the parent thread
                errors.append(error)

        first = threading.Thread(target=run_turn, args=('first', 'start'))
        second = threading.Thread(target=run_turn, args=('second', 'continue'))
        first.start()
        self.assertTrue(first_entered.wait(timeout=5))
        second.start()

        # The first transaction owns sessions ... FOR UPDATE, so the second
        # assistant cannot run until the first draft is committed.
        self.assertFalse(second_entered.wait(timeout=0.3))
        release_first.set()
        first.join(timeout=5)
        second.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(second_entered.is_set())
        self.assertEqual(responses['first']['answer'], 'Что должно быть записано?')
        self.assertEqual(responses['second']['answer'], 'На какую дату?')
        self.assertEqual(responses['second']['pending']['kind'], 'reminder')
        self.assertEqual(responses['second']['pending']['text'], 'Позвонить Ивану')
        self.assertEqual(len(self.rows()), 0)

    def test_model_cannot_claim_creation_without_a_tool(self):
        class HallucinatingAssistant:
            def answer(self, message, history, tools):
                return 'Встреча создана!'
        result = Service(self.repo, HallucinatingAssistant(), self.clock).chat(self.owners[0], uuid4(), 'Запрос')
        self.assertNotIn('Встреча создана', result['answer'])
        self.assertEqual(len(self.rows()), 0)

    def test_model_can_rephrase_clarification(self):
        class RephrasingAssistant:
            def answer(self, message, history, tools):
                tools.call('create_record', {'mode': 'start', 'kind': 'reminder'})
                return 'О чём вам напомнить?'

        result = Service(self.repo, RephrasingAssistant(), self.clock).chat(
            self.owners[0], uuid4(), 'Создай напоминание')

        self.assertEqual(result['answer'], 'О чём вам напомнить?')
        self.assertEqual(result['pending']['kind'], 'reminder')
        self.assertEqual(len(self.rows()), 0)

    def test_model_can_format_read_results_but_creation_stays_server_generated(self):
        created = self.service.chat(self.owners[0], uuid4(), 'Проверить договор')
        self.assertIn('Задача сохранена', created['answer'])
        self.assertNotEqual(created['answer'], 'Ответ модели')

        class FormattingAssistant:
            def answer(self, message, history, tools):
                tools.call('search_records', {'query': 'договор'})
                return 'Нашёл подходящую запись о договоре.'

        result = Service(self.repo, FormattingAssistant(), self.clock).chat(
            self.owners[0], uuid4(), 'Найди договор')

        self.assertEqual(result['answer'], 'Нашёл подходящую запись о договоре.')
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(result['records'][0]['text'], 'Проверить договор')

    def test_import_is_idempotent_and_preserves_chat(self):
        import app.main as main_module
        self.service.chat(self.owners[0], uuid4(), 'Ручная запись')
        first = import_csv(self.repo, self.owners[0], main_module.DATA, 'Europe/Moscow')
        second = import_csv(self.repo, self.owners[0], main_module.DATA, 'Europe/Moscow')
        self.assertEqual(first['imported'], 1000)
        self.assertEqual(second['imported'], 0)
        with self.repo.connect() as con:
            count = con.execute('SELECT count(*) AS n FROM records WHERE owner_id=%s', (self.owners[0],)).fetchone()['n']
            self.assertEqual(count, 1001)
            found = self.repo.search(con, self.owners[0], 'техническое задание')
            self.assertTrue(found)
            self.assertIsNone(found[0]['kind'])

    def test_cancel_pending_does_not_create(self):
        self.service.chat(
            self.owners[0],
            uuid4(),
            'без времени'
        )

        result = self.service.chat(
            self.owners[0],
            uuid4(),
            'отмена'
        )

        self.assertEqual(result['answer'], 'Черновик создания встречи отменён.')
        self.assertIsNone(result['pending'])
        self.assertEqual(len(self.rows()), 0)

    def test_cancel_without_pending_draft_is_explicit(self):
        result = self.service.chat(self.owners[0], uuid4(), 'отмена')
        self.assertEqual(result['answer'], 'Незавершённого черновика нет.')
        self.assertIsNone(result['pending'])
        self.assertEqual(len(self.rows()), 0)

    def test_api_cookie_and_persisted_history(self):
        app = create_app(Settings(database_url=self.repo.dsn, fixed_now='2026-10-27T12:00:00+03:00'),
                         self.repo, ScriptedAssistant())
        with TestClient(app) as client:
            session = client.get('/api/session').json()
            self.owners.append(session['id'])
            response = client.post('/api/chat', json={'message': 'Подготовить отчёт', 'request_id': str(uuid4())})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn('сохранена', response.json()['answer'])
            self.assertEqual(len(client.get('/api/history').json()), 2)
            self.assertEqual(len(client.get('/api/records?period=this_week').json()['records']), 1)
            self.assertEqual(client.get('/api/session').json()['id'], session['id'])

