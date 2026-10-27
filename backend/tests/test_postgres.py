"""Run only against GorkyCode's own DB: TEST_DATABASE_URL must be explicit.

Each test uses fresh session UUIDs and removes only rows belonging to those UUIDs.
"""
import os
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
            tools.call('create_record', {'time_expression': '11:00'})
        elif message == 'без времени':
            tools.call('create_record', {'kind': 'meeting', 'text': 'Иван', 'date_expression': 'завтра'})
        else:
            tools.call('create_record', {'kind': 'task', 'text': message, 'date_expression': 'в пятницу'})
        return 'Ответ модели'


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

    def test_model_cannot_claim_creation_without_a_tool(self):
        class HallucinatingAssistant:
            def answer(self, message, history, tools):
                return 'Встреча создана!'
        result = Service(self.repo, HallucinatingAssistant(), self.clock).chat(self.owners[0], uuid4(), 'Запрос')
        self.assertNotIn('Встреча создана', result['answer'])
        self.assertEqual(len(self.rows()), 0)

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
        self.service.chat(self.owners[0], uuid4(), 'без времени')
        result = self.service.chat(self.owners[0], uuid4(), 'отмена')
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

