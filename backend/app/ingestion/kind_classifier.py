"""Mandatory kind classification for non-interactive imports."""
import logging
import re
from dataclasses import dataclass

from openai import OpenAI


logger = logging.getLogger(__name__)
VALID_KINDS = frozenset({'meeting', 'task', 'reminder'})

SEMANTIC_PROMPT = '''Определи наиболее подходящий тип существующей календарной записи.

Разрешены только: meeting, task, reminder.

meeting — встреча, созвон, планёрка, демо, совещание или другое
запланированное взаимодействие с людьми.
task — действие, которое пользователь должен выполнить.
reminder — запись, основная функция которой — напомнить пользователю о чём-либо.

Примеры:
"Созвон с Иваном" → meeting
"Подготовить отчёт" → task
"Напомнить про оплату" → reminder
"Подготовиться к встрече" → task
"Позвонить Ивану" → task
"Обсудить бюджет с Иваном" → meeting

Обязательно верни ровно одно слово: meeting, task или reminder.
Не возвращай null, unknown, несколько вариантов или пояснение.'''


@dataclass(frozen=True)
class ImportKindResult:
    kind: str
    source: str
    confidence: float
    classifier_result: str | None = None
    fallback_used: bool = False


def normalize_import_text(text: str) -> str:
    return ' '.join(text.strip().lower().replace('ё', 'е').split())


def classify_import_kind_by_rules(text: str) -> str | None:
    """Return only high-confidence kinds, ordered by product priority."""
    value = normalize_import_text(text)
    if re.search(r'^(?:напомнить|напоминание)\b', value):
        return 'reminder'
    if re.search(
        r'^(?:встреча|созвон|демо|совещание)\b|'
        r'^провести\s+план[её]рк\w*\b|'
        r'^обсудить\b.*\s+с\s+\S+',
        value,
    ):
        return 'meeting'
    if re.search(
        r'^(?:подготовить|проверить|отправить|обновить|собрать|передать|'
        r'согласовать|подготовиться|позвонить)\b',
        value,
    ):
        return 'task'
    return None


def ensure_import_kind(value: str | None) -> str:
    return value if value in VALID_KINDS else 'task'


class SemanticImportKindClassifier:
    """Constrained provider call used only after strong rules miss."""
    def __init__(self, settings, client_factory=OpenAI):
        self.settings = settings
        self.client_factory = client_factory
        self.cache = {}

    def __call__(self, text: str) -> str | None:
        key = normalize_import_text(text)
        if key in self.cache:
            return self.cache[key]
        if not self.settings.llm_api_key:
            self.cache[key] = None
            return None
        try:
            with self.client_factory(
                base_url=self.settings.llm_base_url,
                api_key=self.settings.llm_api_key,
                timeout=self.settings.llm_timeout,
                max_retries=0,
            ) as client:
                response = client.chat.completions.create(
                    model=self.settings.llm_model,
                    messages=[
                        {'role': 'system', 'content': SEMANTIC_PROMPT},
                        {'role': 'user', 'content': text},
                    ],
                    temperature=0,
                )
            value = (response.choices[0].message.content or '').strip().lower()
        except Exception as error:  # Import must continue through forced fallback.
            logger.warning('Import semantic classifier failed: %s', type(error).__name__)
            value = None
        result = value if value in VALID_KINDS else None
        self.cache[key] = result
        return result


def classify_import_record(text: str, semantic_classifier=None) -> ImportKindResult:
    ruled = classify_import_kind_by_rules(text)
    if ruled is not None:
        return ImportKindResult(ruled, 'import_rule', 1.0,
                                classifier_result=ruled)
    semantic = None
    if semantic_classifier is not None:
        try:
            semantic = semantic_classifier(text)
        except Exception as error:  # Custom adapters have the same fallback contract.
            logger.warning('Import classifier adapter failed: %s', type(error).__name__)
    if semantic in VALID_KINDS:
        return ImportKindResult(semantic, 'import_semantic_classifier', 0.8,
                                classifier_result=semantic)
    return ImportKindResult(
        ensure_import_kind(semantic), 'import_fallback', 0.0,
        classifier_result=semantic, fallback_used=True,
    )
