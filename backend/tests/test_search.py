import json
import unittest
from types import SimpleNamespace

from app.assistant.search import HybridSearch


class _Embeddings:
    def create(self, **kwargs):
        vectors = ([1.0, 0.0], [0.95, 0.05], [0.1, 0.9])
        return SimpleNamespace(data=[SimpleNamespace(embedding=value) for value in vectors])


class _Completions:
    def create(self, **kwargs):
        arguments = json.dumps({'candidate_number': 1, 'confidence': 'high'})
        call = SimpleNamespace(function=SimpleNamespace(arguments=arguments))
        message = SimpleNamespace(tool_calls=[call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class _Client:
    embeddings = _Embeddings()
    chat = SimpleNamespace(completions=_Completions())


class HybridSearchTests(unittest.TestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            semantic_search=True, llm_api_key='test', embedding_model='test',
            llm_base_url='http://example.invalid', llm_timeout=1,
            llm_model='test',
        )
        self.search = HybridSearch(self.settings, client_factory=lambda **kwargs: _Client())

    def test_semantic_search_returns_only_supplied_database_rows(self):
        rows = [
            {'id': 'real-1', 'text': 'встреча с мамой'},
            {'id': 'real-2', 'text': 'встреча с Машей'},
        ]
        ranked = self.search.semantic_candidates('встреча с мамулей', rows)
        self.assertEqual([row['id'] for row in ranked], ['real-1'])

    def test_reranker_can_only_select_numbered_candidate(self):
        rows = [
            {'id': 'real-1', 'text': 'встреча с мамой'},
            {'id': 'real-2', 'text': 'созвон с мамой'},
        ]
        ranked = self.search.rerank('встреча с мамулей', rows)
        self.assertEqual(ranked, [rows[0]])


if __name__ == '__main__':
    unittest.main()
