"""Deterministic canonical record-title cleanup."""
import json
import logging
import re


logger = logging.getLogger(__name__)


def _remove_fragment(value: str, fragment: str) -> str:
    if not fragment.strip():
        return value
    return re.sub(re.escape(fragment.strip()), ' ', value, flags=re.IGNORECASE)


def clean_title(text: str | None, raw_text: str,
                temporal_evidence=()) -> str:
    """Keep semantic title text and remove command/temporal scaffolding."""
    original = text or ''
    value = original.strip() or raw_text.strip()
    evidence = tuple(item for item in temporal_evidence if item)
    for fragment in evidence:
        value = _remove_fragment(value, fragment)

    value = re.sub(
        r'^\s*(?:создай|создать|поставь|добавь|назначь|напомни)\s+',
        '', value, flags=re.IGNORECASE,
    )
    # Accusative kind labels and "созвон" directly after an action are service
    # markers. Nominative semantic titles such as "встреча с Дашей" survive.
    value = re.sub(
        r'^\s*(?:встречу|задачу|напоминание|созвон)\s+',
        '', value, flags=re.IGNORECASE,
    )

    # Evidence is authoritative; these finite fallbacks cover providers that
    # omitted it while keeping the cleanup limited to temporal syntax.
    temporal_patterns = (
        r'\b(?:сегодня|завтра|послезавтра)\b',
        r'\b(?:в|на)\s+(?:[01]?\d|2[0-3])(?::[0-5]\d)?\b',
        r'\b(?:на\s+)?\d{1,2}[.]\d{1,2}(?:[.]\d{2,4})?\b',
        r'\bдо конца (?:недели|месяца)\b',
        r'\bна (?:эту|следующую) (?:неделю|месяц)\b',
        r'\b\d{1,2}[.]\d{1,2}\s*[–—-]\s*\d{1,2}[.]\d{1,2}\b',
        r'\b(?:на\s+)?(?:\d+|один|два|три|четыре)\s*'
        r'(?:минут\w*|час\w*)\b',
    )
    for pattern in temporal_patterns:
        value = re.sub(pattern, ' ', value, flags=re.IGNORECASE)

    value = re.sub(
        r'[,;:]?\s*(?:назначь|поставь|добавь)\s+(?:его\s+)?на\b.*$',
        ' ', value, flags=re.IGNORECASE,
    )
    value = re.sub(r'[,;:]?\s*его\s+на\b.*$', ' ', value, flags=re.IGNORECASE)
    value = re.sub(r'\b(на|в)\s+\1\b', r'\1', value, flags=re.IGNORECASE)
    value = re.sub(r'\s+', ' ', value).strip(' ,.-')
    value = re.sub(r'\s+(?:на|в|до)$', '', value, flags=re.IGNORECASE).strip(' ,.-')

    logger.info('title_cleanup %s', json.dumps({
        'raw_text': raw_text,
        'llm_text': original,
        'temporal_evidence': list(evidence),
        'cleaned_text': value,
    }, ensure_ascii=False))
    return value
