import os
import unittest
from pathlib import Path

from app.ingestion.csv_import import read_csv
from app.ingestion.kind_classifier import (
    classify_import_kind_by_rules, classify_import_record,
    normalize_import_text,
)


class ImportKindTests(unittest.TestCase):
    def test_normalize_import_text_is_conservative(self):
        self.assertEqual(
            normalize_import_text('  Демо   проекта по Ёлке  '),
            'демо проекта по елке',
        )

    def test_meeting_rules(self):
        for text in (
            'Созвон с Партнёром по интеграции',
            'Провести планёрку по бюджету',
            'Демо проекта по хакатону',
            'Встреча с Иваном',
            'Обсудить бюджет с Иваном',
        ):
            self.assertEqual(classify_import_kind_by_rules(text), 'meeting', text)

    def test_reminder_rules_have_priority_over_meeting_words(self):
        for text in (
            'Напомнить Анной про CRM',
            'Напомнить Дмитрием про мобильному приложению',
            'Напомнить про встречу с Иваном',
        ):
            self.assertEqual(classify_import_kind_by_rules(text), 'reminder', text)

    def test_task_rules_include_meeting_preparation(self):
        for text in (
            'Подготовить техническое задание',
            'Отправить отчёт Алексеем',
            'Проверить сроки по новому продукту',
            'Подготовиться к встрече с Партнёром',
            'Позвонить Иваном',
        ):
            self.assertEqual(classify_import_kind_by_rules(text), 'task', text)

    def test_semantic_classifier_is_constrained_to_valid_kind(self):
        result = classify_import_record(
            'Синхронизация по продукту', lambda text: 'meeting',
        )
        self.assertEqual(result.kind, 'meeting')
        self.assertEqual(result.source, 'import_semantic_classifier')
        self.assertFalse(result.fallback_used)

    def test_invalid_or_failed_semantic_classifier_forces_task(self):
        invalid = classify_import_record(
            'Синхронизация по продукту', lambda text: 'unknown',
        )
        self.assertEqual(invalid.kind, 'task')
        self.assertEqual(invalid.source, 'import_fallback')
        self.assertTrue(invalid.fallback_used)

        def failed(_):
            raise RuntimeError('offline')

        failed_result = classify_import_record('Без категории', failed)
        self.assertEqual(failed_result.kind, 'task')
        self.assertEqual(failed_result.source, 'import_fallback')

    def test_all_demo_rows_receive_one_valid_kind(self):
        root = Path(os.getenv(
            'DATA_DIR', str(Path(__file__).resolve().parents[2] / 'data'),
        ))
        rows = read_csv(root / 'voice_assistant_events_1000_77e927b5d2.csv')
        results = [
            classify_import_record(raw['text'], lambda text: 'meeting')
            for raw, _, _ in rows
        ]
        self.assertEqual(len(results), 1000)
        self.assertTrue(all(item.kind in {'meeting', 'task', 'reminder'}
                            for item in results))
        self.assertEqual(sum(item.fallback_used for item in results), 0)
        self.assertTrue({'meeting', 'task', 'reminder'} <=
                        {item.kind for item in results})


if __name__ == '__main__':
    unittest.main()
