"""Contracts for the future operation flow. Not connected to the running chat.

Interpreter output is a proposal, never permission to write. Only server-side
planning and policy checks may produce an executable plan.
"""
from datetime import date, datetime, time
from enum import Enum
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import Kind


OperationField = Literal[
    'kind', 'text', 'date_expression', 'time_expression', 'timezone', 'duration',
]
DurationStatus = Literal[
    'missing', 'provided', 'declined_to_specify', 'defaulted',
]


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, str_strip_whitespace=True)


class Action(str, Enum):
    CREATE = 'create'
    UPDATE = 'update'
    CANCEL = 'cancel'
    READ = 'read'
    UNKNOWN = 'unknown'


class TurnKind(str, Enum):
    NEW = 'new'
    CONTINUE = 'continue'
    CONFIRM = 'confirm'
    REJECT = 'reject'
    ABANDON = 'abandon'  # Discard the pending operation, not the saved record.
    CORRECTION = 'correction'
    UNKNOWN = 'unknown'


class Phase(str, Enum):
    NEED_INTENT = 'need_intent'
    NEED_TARGET = 'need_target'
    NEED_FIELDS = 'need_fields'
    NEED_CONFIRMATION = 'need_confirmation'
    READY = 'ready'
    COMPLETED = 'completed'
    REJECTED = 'rejected'


class DurationInput(Contract):
    """What the user actually said, without calendar-policy estimates."""
    type: Literal['exact', 'approximate', 'range', 'qualitative'] | None = None
    minutes: int | None = Field(default=None, ge=5, le=480)
    min_minutes: int | None = Field(default=None, ge=5, le=480)
    max_minutes: int | None = Field(default=None, ge=5, le=480)
    value: Literal['long', 'short'] | None = None
    evidence: str | None = None
    # Compatibility input for pending states created before user_duration.
    description: str | None = None

    @model_validator(mode='after')
    def validate_shape(self):
        kind = self.type
        if kind is None:
            if self.minutes is not None:
                kind = 'exact'
                object.__setattr__(self, 'type', kind)
            elif self.description:
                kind = 'qualitative'
                object.__setattr__(self, 'type', kind)
                object.__setattr__(self, 'value', self.value or 'long')
        if kind in {'exact', 'approximate'} and self.minutes is None:
            raise ValueError(f'{kind} duration requires minutes')
        if kind == 'range':
            if self.min_minutes is None or self.max_minutes is None:
                raise ValueError('range duration requires both bounds')
            if self.min_minutes > self.max_minutes:
                raise ValueError('duration range is reversed')
        if kind == 'qualitative' and self.value is None:
            raise ValueError('qualitative duration requires value')
        return self


class DurationValue(DurationInput):
    """Technical calendar blocking retained under the legacy field name."""
    source: Literal['user', 'estimated', 'unknown'] = 'unknown'
    blocking_source: Literal[
        'user_exact', 'user_approximate', 'user_range_max',
        'qualitative_default', 'event_type_default', 'legacy_unknown',
    ] = 'legacy_unknown'


class RecordPatch(Contract):
    """Only supplied fields change. Missing and explicit null are different.

    Adapters must use model_fields_set/model_dump(exclude_unset=True), not
    exclude_none=True. Explicit null requests clearing a value; policy decides
    whether this is legal for the resulting record kind.
    """
    kind: Kind | None = None
    text: str | None = Field(default=None, min_length=1, max_length=1000)
    date_expression: str | None = None
    time_expression: str | None = None
    timezone: str | None = None
    duration: DurationInput | None = None


class TargetQuery(Contract):
    """Filters refer to the existing record, not to proposed new field values."""
    query: str | None = None
    kind: Kind | None = None
    temporal: Literal['future', 'past', 'all', 'unspecified'] = 'unspecified'
    date_expression: str | None = None
    time_expression: str | None = None


class OperationIntent(Contract):
    action: Action = Action.UNKNOWN
    # Original command and source excerpts survive while a clarification is pending.
    source_text: str | None = None
    field_evidence: dict[str, str] = Field(default_factory=dict)
    target: TargetQuery = Field(default_factory=TargetQuery)
    changes: RecordPatch = Field(default_factory=RecordPatch)
    read_fields: tuple[str, ...] = ()
    # Explicitly means "the user does not know this value". This differs from
    # an omitted field and from an explicit request to clear a stored value.
    unknown_fields: tuple[OperationField, ...] = ()
    # The user's phrase contains a value, but its semantic role is not unique.
    # Unlike unknown_fields, this asks the user to choose between interpretations.
    ambiguous_fields: tuple[OperationField, ...] = ()
    ambiguity_values: dict[OperationField, str] = Field(default_factory=dict)
    ambiguity_evidence: str | None = None
    # Transient selection made on the current turn; state.merge consumes it.
    resolved_ambiguity: OperationField | None = None
    # Transient negotiation signal for a different proposed calendar slot.
    alternative_requested: bool = False


class Interpretation(Contract):
    """Untrusted proposal from the language model; server verifies continuation."""
    turn: TurnKind = TurnKind.UNKNOWN
    intent: OperationIntent | None = None
    selected_candidate_id: UUID | None = None


class RecordSnapshot(Contract):
    id: UUID
    # Imported records stay untyped until a separate explicit decision.
    kind: Kind | None
    text: str
    scheduled_date: date | None
    scheduled_time: time | None
    timezone: str
    user_duration: DurationInput | None = None
    duration_status: DurationStatus = 'defaulted'
    duration: DurationValue = Field(default_factory=DurationValue)
    status: Literal['open', 'done', 'cancelled']
    revision: str  # Server adapter derives this from updated_at/version.


class TargetResolution(Contract):
    candidates: tuple[RecordSnapshot, ...] = ()
    truncated: bool = False


class ChangePlan(Contract):
    """Server-built before/after state; update preserves identity and omitted fields."""
    action: Literal['create', 'update', 'cancel']
    before: RecordSnapshot | None
    after: RecordSnapshot
    fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')

    @model_validator(mode='after')
    def validate_shape(self):
        if self.action == 'create' and self.before is not None:
            raise ValueError('create plan cannot have a previous record')
        if self.action != 'create' and self.before is None:
            raise ValueError(f'{self.action} plan requires a previous record')
        if self.before is not None and self.before.id != self.after.id:
            raise ValueError('an operation cannot replace a record identity')
        if self.action == 'cancel' and self.after.status != 'cancelled':
            raise ValueError('cancel plan must produce cancelled status')
        if self.action == 'create' and self.after.status != 'open':
            raise ValueError('create plan must produce open status')
        if self.action == 'update' and self.before.status != self.after.status:
            raise ValueError('update plan cannot change record status')
        if self.action == 'cancel':
            before = self.before.model_dump(exclude={'status', 'revision'})
            after = self.after.model_dump(exclude={'status', 'revision'})
            if before != after:
                raise ValueError('cancel plan cannot change record fields')
        return self


class ValidationIssue(Contract):
    code: str
    field: str | None = None
    message: str


class PolicyDecision(Contract):
    phase: Phase
    issues: tuple[ValidationIssue, ...] = ()
    missing_fields: tuple[str, ...] = ()
    question: str | None = None
    alternative_plan: ChangePlan | None = None
    alternative_intent: OperationIntent | None = None

    @model_validator(mode='after')
    def validate_alternative(self):
        if (self.alternative_plan is None) != (self.alternative_intent is None):
            raise ValueError('alternative plan and intent must be supplied together')
        if self.alternative_plan is not None and self.phase != Phase.NEED_CONFIRMATION:
            raise ValueError('an alternative plan must require confirmation')
        return self


class PendingChange(Contract):
    """A conflicting patch value awaiting an explicit yes/no decision."""
    field: OperationField
    old_value: Any
    new_value: Any


class PendingOperation(Contract):
    """Versioned replacement for creation-only drafts; persistence adapter is TODO."""
    schema_version: Literal[1] = 1
    operation_id: UUID
    phase: Phase
    intent: OperationIntent
    candidate_ids: tuple[UUID, ...] = ()
    selected_record_id: UUID | None = None
    proposed_plan: ChangePlan | None = None
    confirmed_fingerprint: str | None = None
    pending_changes: tuple[PendingChange, ...] = ()
    created_at: datetime
    updated_at: datetime
    expires_at: datetime

    @model_validator(mode='after')
    def validate_times(self):
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError('operation timestamps must be timezone-aware')
        if self.updated_at < self.created_at or self.expires_at <= self.updated_at:
            raise ValueError('invalid operation timestamps')
        return self


class ExecutionResult(Contract):
    action: Literal['create', 'update', 'cancel']
    records: tuple[RecordSnapshot, ...] = Field(min_length=1)


class OperationOutcome(Contract):
    phase: Phase
    pending: PendingOperation | None = None
    question: str | None = None
    issues: tuple[ValidationIssue, ...] = ()
    records: tuple[RecordSnapshot, ...] = ()
    read_fields: tuple[str, ...] = ()
    execution: ExecutionResult | None = None

    @model_validator(mode='after')
    def validate_result(self):
        if self.execution is not None and self.phase != Phase.COMPLETED:
            raise ValueError('execution facts require a completed operation')
        if self.execution is not None and self.records != self.execution.records:
            raise ValueError('outcome records must match execution facts')
        if self.pending is not None and self.phase in (Phase.COMPLETED, Phase.REJECTED):
            raise ValueError('terminal outcome cannot keep pending state')
        return self


def plan_fingerprint(action: str, before: RecordSnapshot | None,
                     after: RecordSnapshot) -> str:
    """Return the canonical server-side identity of a proposed mutation.

    The previous revision is part of ``before``. Therefore a confirmation cannot
    silently authorize the same-looking change after the record was modified.
    """
    payload = {
        'action': action,
        'before': before.model_dump(mode='json') if before else None,
        'after': after.model_dump(mode='json'),
    }
    import json
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    return sha256(encoded).hexdigest()


def make_change_plan(action: Literal['create', 'update', 'cancel'],
                     before: RecordSnapshot | None,
                     after: RecordSnapshot) -> ChangePlan:
    """Build a plan whose fingerprint cannot be supplied by the model/client."""
    return ChangePlan(action=action, before=before, after=after,
                      fingerprint=plan_fingerprint(action, before, after))
