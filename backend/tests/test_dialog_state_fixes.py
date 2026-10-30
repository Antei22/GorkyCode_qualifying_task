"""Regression coverage for canonical text, current-turn duration and person guards."""
import unittest
from datetime import datetime
from uuid import uuid4

from app.assistant.operation_interpreter import LLMOperationInterpreter, Proposal
from app.application.operations.ports import TurnContext
from app.domain.duration import parse_user_duration
from app.domain.operations import Action, OperationIntent, PendingOperation, Phase, RecordPatch
from app.domain.text import build_canonical_text, extract_person_constraint, person_matches


class DialogStateFixTests(unittest.TestCase):
    def test_meeting_title_is_canonical_before_persistence(self):
        self.assertEqual(
            build_canonical_text(
                'meeting', 'на с подругой',
                'Добавь встречу на завтра с подругой', ('завтра',),
            ),
            'встреча с подругой',
        )

    def test_duration_range_is_parsed_from_current_reply(self):
        pending = PendingOperation(
            operation_id=uuid4(), phase=Phase.NEED_FIELDS,
            intent=OperationIntent(
                action=Action.CREATE,
                changes=RecordPatch(
                    kind='meeting', text='встреча с Максимом',
                    date_expression='2026-10-31', time_expression='17:00',
                ),
            ),
            created_at=datetime.fromisoformat('2026-10-30T12:00:00+03:00'),
            updated_at=datetime.fromisoformat('2026-10-30T12:00:00+03:00'),
            expires_at=datetime.fromisoformat('2026-10-30T12:30:00+03:00'),
        )
        proposal = Proposal(turn='continue', action='unknown')
        result = LLMOperationInterpreter._apply_expected_duration(
            'час-два', pending, proposal,
        )
        self.assertEqual(result.user_duration.min_minutes, 60)
        self.assertEqual(result.user_duration.max_minutes, 120)
        self.assertEqual(parse_user_duration('час-два')['type'], 'range')

    def test_person_constraint_does_not_match_another_person(self):
        person = extract_person_constraint('Есть ли у меня что-то с Иваном')
        self.assertEqual(person, 'иван')
        self.assertTrue(person_matches(person, 'встреча с Иваном'))
        self.assertFalse(person_matches(person, 'встреча с Михаилом'))

    def test_current_turn_without_date_has_no_date_evidence(self):
        self.assertEqual(LLMOperationInterpreter._date_phrases('хз'), ())


if __name__ == '__main__':
    unittest.main()
