import unittest
from types import SimpleNamespace
from app.assistant.agent import Assistant
from app.assistant.tools import CreateRecord, ListRecords, SearchRecords
from app.config import Settings
from app.domain.models import DomainError
from app.domain.time import Clock


class Reply:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls

    def model_dump(self, **kwargs):
        return {'role': 'assistant', 'content': self.content, 'tool_calls': []}


def tool(name, arguments='{}'):
    return SimpleNamespace(id='call', function=SimpleNamespace(name=name, arguments=arguments))


class FakeClient:
    def __init__(self, replies):
        self.replies, self.requests = iter(replies), []
        self.chat = SimpleNamespace(completions=self)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=next(self.replies))])


class FakeTools:
    clock = Clock(fixed_now='2026-10-27T12:00:00+03:00')
    pending = None
    def __init__(self):
        self.calls = []
        self.questions = []
        self.created = []
        self.errors = []

    def encode(self, result):
        import json
        return json.dumps(result)

    def call(self, name, args):
        self.calls.append((name, args))
        return {'ok': True}


class AssistantTests(unittest.TestCase):
    def test_russian_kind_aliases_are_normalized_for_all_tools(self):
        self.assertEqual(CreateRecord.model_validate({'kind': 'встреча'}).kind, 'meeting')
        self.assertEqual(CreateRecord.model_validate({'kind': 'задачу'}).kind, 'task')
        self.assertEqual(CreateRecord.model_validate({'kind': 'напоминание'}).kind, 'reminder')
        self.assertEqual(ListRecords.model_validate({'period': 'all',
                                                     'kind': 'встречу'}).kind, 'meeting')
        self.assertEqual(SearchRecords.model_validate({'query': 'Иван',
                                                       'kind': 'задача'}).kind, 'task')

    def test_search_then_create_is_supported(self):
        client = FakeClient([Reply(tool_calls=[tool('search_records', '{"query":"Иван"}')]),
                             Reply(tool_calls=[tool('create_record', '{"kind":"reminder"}')]),
                             Reply(content='Готово')])
        tools = FakeTools()
        agent = Assistant(Settings(llm_api_key='test'), lambda **kwargs: client)
        self.assertEqual(agent.answer('Найди и напомни', [], tools), 'Готово')
        self.assertEqual([name for name, _ in tools.calls], ['search_records', 'create_record'])
        self.assertEqual(len(client.requests), 3)

    def test_tool_loop_is_bounded(self):
        client = FakeClient([Reply(tool_calls=[tool('search_records')]) for _ in range(6)])
        with self.assertRaises(DomainError):
            Assistant(Settings(llm_api_key='test'), lambda **kwargs: client).answer('Запрос', [], FakeTools())

    def test_missing_credentials_fail_explicitly(self):
        with self.assertRaisesRegex(DomainError, 'LLM_API_KEY'):
            Assistant(Settings(llm_api_key='')).answer('Запрос', [], FakeTools())

    def test_plain_text_without_tool_is_retried_and_clarification_stops_loop(self):
        client = FakeClient([
            Reply(content='Обычный ответ без инструмента'),
            Reply(tool_calls=[tool('create_record', '{"kind":"meeting"}')]),
            Reply(content='Во сколько назначить встречу?'),
        ])
        tools = FakeTools()

        def call(name, args):
            tools.calls.append((name, args))
            tools.questions.append('На какое время?')
            return {'needs_clarification': True, 'missing_field': 'time'}

        tools.call = call
        answer = Assistant(Settings(llm_api_key='test'),
                           lambda **kwargs: client).answer('Создай встречу', [], tools)

        self.assertEqual(answer, 'Во сколько назначить встречу?')
        self.assertEqual([request['tool_choice'] for request in client.requests],
                         ['auto', 'required', 'none'])
        self.assertEqual(len(tools.calls), 1)

    def test_successful_creation_does_not_request_model_again(self):
        client = FakeClient([Reply(tool_calls=[tool(
            'create_record', '{"kind":"task","text":"Купить билеты"}')])])
        tools = FakeTools()

        def call(name, args):
            tools.calls.append((name, args))
            tools.created.append({'kind': 'task', 'text': 'Купить билеты'})
            return {'created': tools.created[0]}

        tools.call = call
        answer = Assistant(Settings(llm_api_key='test'),
                           lambda **kwargs: client).answer('Купить билеты', [], tools)

        self.assertEqual(answer, '')
        self.assertEqual(len(client.requests), 1)

