import unittest
from types import SimpleNamespace
from app.assistant.agent import Assistant
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
    questions = []

    def __init__(self):
        self.calls = []

    def encode(self, result):
        import json
        return json.dumps(result)

    def call(self, name, args):
        self.calls.append((name, args))
        return {'ok': True}


class AssistantTests(unittest.TestCase):
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

