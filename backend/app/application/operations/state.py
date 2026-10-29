"""Pure state helpers for the unified operation dialogue."""
from app.domain.operations import (
    Action, OperationIntent, PendingOperation, RecordPatch, TargetQuery,
)


class InvalidTransition(ValueError):
    """A recoverable dialogue transition rejected by the server."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _overlay(previous, current, model_type):
    values = previous.model_dump(exclude_unset=True)
    values.update(current.model_dump(exclude_unset=True))
    return model_type.model_validate(values)


def merge_intent(previous: OperationIntent,
                 current: OperationIntent | None) -> OperationIntent:
    """Merge a clarification without turning omitted fields into nulls."""
    if current is None:
        return previous
    if current.action not in (Action.UNKNOWN, previous.action):
        raise InvalidTransition(
            'action_changed',
            'Новую команду нужно начать отдельно, не продолжая прежнее уточнение.',
        )
    action = previous.action if current.action == Action.UNKNOWN else current.action
    read_fields = current.read_fields or previous.read_fields
    unknown_fields = set(previous.unknown_fields)
    unknown_fields.update(current.unknown_fields)
    # A concrete answer supersedes an earlier "unknown" response.
    unknown_fields.difference_update(current.changes.model_fields_set)
    ambiguous_fields = set(previous.ambiguous_fields)
    ambiguity_values = dict(previous.ambiguity_values)
    ambiguity_evidence = previous.ambiguity_evidence
    if current.resolved_ambiguity is not None:
        ambiguous_fields.clear()
        ambiguity_values.clear()
        ambiguity_evidence = None
    else:
        ambiguous_fields.update(current.ambiguous_fields)
        ambiguity_values.update(current.ambiguity_values)
        ambiguity_evidence = current.ambiguity_evidence or ambiguity_evidence
        ambiguous_fields.difference_update(current.changes.model_fields_set)
        ambiguous_fields.difference_update(current.unknown_fields)
    unknown_fields.difference_update(current.ambiguous_fields)
    change_values = previous.changes.model_dump(exclude_unset=True)
    change_values.update(current.changes.model_dump(exclude_unset=True))
    # Conversely, a new explicit "unknown" response must not retain a stale
    # concrete value for the same field.
    for field in current.unknown_fields:
        change_values.pop(field, None)
    for field in current.ambiguous_fields:
        change_values.pop(field, None)
    return OperationIntent(
        action=action,
        target=_overlay(previous.target, current.target, TargetQuery),
        changes=RecordPatch.model_validate(change_values),
        read_fields=read_fields,
        unknown_fields=tuple(sorted(unknown_fields)),
        ambiguous_fields=tuple(sorted(ambiguous_fields)),
        ambiguity_values=ambiguity_values,
        ambiguity_evidence=ambiguity_evidence,
        # A request for another slot belongs only to the current turn.
        alternative_requested=current.alternative_requested,
        calendar_scope=current.calendar_scope or previous.calendar_scope,
    )


def pending_is_expired(pending: PendingOperation, now) -> bool:
    return now >= pending.expires_at
