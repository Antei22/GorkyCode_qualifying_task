from datetime import date, time
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Kind = Literal['task', 'meeting', 'reminder']


class RecordDraft(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    kind: Kind
    kind_source: Literal[
        'import_rule', 'import_semantic_classifier', 'import_fallback',
        'user_input', 'explicit_user_command', 'explicit_entity_word',
        'semantic_inference', 'user_clarification', 'system_default',
    ] = 'user_input'
    text: str = Field(min_length=1, max_length=1000)
    scheduled_date: date | None = None
    scheduled_end_date: date | None = None
    scheduled_time: time | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=480)
    duration_source: Literal['user', 'estimated', 'unknown'] = 'unknown'
    duration_description: str | None = None
    user_duration: dict | None = None
    duration_status: Literal[
        'missing', 'provided', 'declined_to_specify', 'defaulted'
    ] = 'defaulted'
    blocking_duration_source: str = 'legacy_unknown'
    timezone: str

    @model_validator(mode='after')
    def preserve_legacy_duration_fact(self):
        if (self.user_duration is None and self.duration_source == 'user'
                and self.duration_minutes is not None):
            self.user_duration = {
                'type': 'exact', 'minutes': self.duration_minutes,
                'evidence': self.duration_description,
            }
        if self.user_duration is not None and self.duration_status == 'defaulted':
            self.duration_status = 'provided'
        if self.blocking_duration_source == 'legacy_unknown':
            if self.duration_source == 'user' and self.duration_minutes is not None:
                self.blocking_duration_source = 'user_exact'
            elif self.duration_source == 'estimated':
                self.blocking_duration_source = (
                    'qualitative_default' if self.duration_description
                    else 'event_type_default'
                )
        return self


class DomainError(ValueError):
    """A recoverable input error, safe to show to the user."""


class FieldInputError(DomainError):
    """A recoverable value error tied to one operation input field."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field

