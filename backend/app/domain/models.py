from datetime import date, time
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

Kind = Literal['task', 'meeting', 'reminder']


class RecordDraft(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    kind: Kind
    text: str = Field(min_length=1, max_length=1000)
    scheduled_date: date | None = None
    scheduled_time: time | None = None
    timezone: str


class DomainError(ValueError):
    """A recoverable input error, safe to show to the user."""

