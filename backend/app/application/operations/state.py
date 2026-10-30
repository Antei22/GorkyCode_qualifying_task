"""Pure state helpers for the unified operation dialogue."""
from app.domain.operations import (
    Action, OperationIntent, PendingChange, PendingOperation, RecordPatch,
    TargetQuery,
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
    field_sources = dict(previous.field_sources)
    field_sources.update(current.field_sources)
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
    if 'date_expression' in current.changes.model_fields_set:
        change_values.pop('temporal', None)
    elif 'temporal' in current.changes.model_fields_set:
        change_values.pop('date_expression', None)
    # Conversely, a new explicit "unknown" response must not retain a stale
    # concrete value for the same field.
    for field in current.unknown_fields:
        change_values.pop(field, None)
    for field in current.ambiguous_fields:
        change_values.pop(field, None)
    kind_selected = 'kind' in current.changes.model_fields_set
    return OperationIntent(
        action=action,
        source_text=previous.source_text or current.source_text,
        field_evidence={**previous.field_evidence, **current.field_evidence},
        field_sources=field_sources,
        semantic_class=current.semantic_class or previous.semantic_class,
        kind_candidates=(() if kind_selected else
                         current.kind_candidates or previous.kind_candidates),
        kind_source=(current.kind_source if kind_selected and current.kind_source
                     else previous.kind_source),
        target=_overlay(previous.target, current.target, TargetQuery),
        changes=RecordPatch.model_validate(change_values),
        read_fields=read_fields,
        unknown_fields=tuple(sorted(unknown_fields)),
        ambiguous_fields=tuple(sorted(ambiguous_fields)),
        ambiguity_values=ambiguity_values,
        ambiguity_evidence=ambiguity_evidence,
        all_matches=current.all_matches or previous.all_matches,
        # A request for another slot belongs only to the current turn.
        alternative_requested=current.alternative_requested,
    )


def merge_patch(previous: OperationIntent, current: OperationIntent | None,
                allow_overwrite: bool = False):
    """Accept empty/same fields and isolate differing draft values.

    Values are already canonical by this boundary: dates are ISO dates, times are
    HH:MM and durations are structured models. Equality is therefore semantic,
    not a comparison of the original LLM JSON representation.
    """
    if current is None:
        return previous, (), (), ()
    previous_values = previous.changes.model_dump(exclude_unset=True, mode='json')
    current_values = current.changes.model_dump(exclude_unset=True, mode='json')
    accepted = {}
    conflicts = []
    same = []
    for field, new_value in current_values.items():
        old_source = previous.field_sources.get(field, 'user_input')
        new_source = current.field_sources.get(field, 'user_input')
        if field not in previous_values or previous_values[field] is None:
            accepted[field] = new_value
        elif previous_values[field] == new_value:
            same.append(field)
        elif allow_overwrite or (
                old_source == 'system_default' and new_source == 'user_input'):
            accepted[field] = new_value
        else:
            conflicts.append(PendingChange(
                field=field,
                old_value=previous_values[field],
                new_value=new_value,
                old_source=old_source,
                new_source=new_source,
            ))
    safe_current = current.model_copy(update={
        'changes': RecordPatch.model_validate(accepted),
        'field_sources': {
            field: current.field_sources.get(field, 'user_input')
            for field in accepted
        },
    })
    return (merge_intent(previous, safe_current), tuple(accepted), tuple(same),
            tuple(conflicts))


def apply_pending_changes(intent: OperationIntent,
                          changes: tuple[PendingChange, ...]):
    patch = RecordPatch.model_validate({
        change.field: change.new_value for change in changes
    })
    return merge_intent(intent, OperationIntent(
        changes=patch,
        field_sources={change.field: change.new_source for change in changes},
    ))


def pending_is_expired(pending: PendingOperation, now) -> bool:
    return now >= pending.expires_at
