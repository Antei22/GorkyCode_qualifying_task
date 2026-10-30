"""Versioned JSON boundary for persisting PendingOperation in a session."""
from typing import Any

from app.domain.operations import PendingOperation


class UnsupportedOperationState(ValueError):
    pass


def dump_pending(pending: PendingOperation) -> dict[str, Any]:
    """Preserve unset patch fields recursively; explicit null remains present."""
    payload = pending.model_dump(mode='json', exclude_unset=True)
    # A default would normally be omitted by exclude_unset, but the discriminator
    # must always be present so legacy session payloads are never guessed.
    payload['schema_version'] = pending.schema_version
    return payload


def load_pending(payload: object) -> PendingOperation:
    if not isinstance(payload, dict) or payload.get('schema_version') != 1:
        raise UnsupportedOperationState('unsupported or legacy operation state')
    return PendingOperation.model_validate(payload)
