"""Readable, request-scoped diagnostics for the assistant pipeline."""
import json
import logging
from datetime import date, datetime, time
from enum import Enum
from uuid import UUID


PIPELINE_LOGGER = 'gorkycode.pipeline'
logger = logging.getLogger(PIPELINE_LOGGER)


def configure_pipeline_logging(enabled: bool) -> None:
    """Send pipeline events to the container stdout exactly once."""
    logger.setLevel(logging.INFO if enabled else logging.WARNING)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter('%(levelname)s %(name)s %(message)s'))
        logger.addHandler(handler)
    logger.propagate = False


def _json_value(value):
    if isinstance(value, (UUID, datetime, date, time, Enum)):
        return str(value.value if isinstance(value, Enum) else value)
    if hasattr(value, 'model_dump'):
        return value.model_dump(mode='json', exclude_none=True)
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f'unsupported log value: {type(value).__name__}')


def pipeline_event(stage: str, event: str, context=None, **details) -> None:
    """Write one machine-filterable event without credentials or SQL secrets."""
    payload = {'stage': stage, 'event': event}
    if context is not None:
        payload['request_id'] = str(context.request_id)
        payload['owner_id'] = str(context.owner_id)
    payload.update(details)
    logger.info('PIPELINE %s', json.dumps(
        payload, ensure_ascii=False, default=_json_value, separators=(',', ':')
    ))
