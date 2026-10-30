"""Intent, precision and provenance rules for meeting duration.

``user_duration`` is a fact supplied by the user.  Calendar blocking is a
separate server policy and must never be presented as that fact.
"""
import re


_NUMBER_WORDS = {
    'один': 1, 'одну': 1, 'одна': 1, 'два': 2, 'две': 2, 'три': 3,
    'четыре': 4, 'пять': 5, 'шесть': 6, 'семь': 7, 'восемь': 8,
    'девять': 9, 'десять': 10, 'пятнадцать': 15, 'двадцать': 20,
    'тридцать': 30, 'сорок': 40, 'пятьдесят': 50,
}


def _numeric(value):
    normalized = value.lower().replace('ё', 'е')
    for word, number in _NUMBER_WORDS.items():
        normalized = re.sub(rf'\b{word}\b', str(number), normalized)
    normalized = re.sub(
        r'\b(20|30|40|50)\s+([1-9])\b',
        lambda match: str(int(match[1]) + int(match[2])), normalized,
    )
    return normalized


def _minutes(number, unit):
    return int(number) * (60 if unit.startswith('час') else 1)


def parse_user_duration(message):
    """Return a JSON-ready, source-grounded user duration or ``None``.

    The parser verifies common numeric forms after the LLM has identified the
    semantic field.  It deliberately keeps ranges and approximate values
    distinct instead of collapsing them into a single exact minute count.
    """
    original = message.strip()
    value = _numeric(original)

    special_range = re.search(r'\bчас\s*[-–—]\s*(?:два|2)\b', original.lower())
    if special_range:
        return {'type': 'range', 'min_minutes': 60, 'max_minutes': 120,
                'evidence': special_range.group(0)}
    range_match = re.search(
        r'\b(\d+)\s*[-–—]\s*(\d+)\s*(минут\w*|час\w*)\b', value,
    )
    if range_match:
        low = _minutes(range_match[1], range_match[3])
        high = _minutes(range_match[2], range_match[3])
        if 5 <= low <= high <= 480:
            return {'type': 'range', 'min_minutes': low, 'max_minutes': high,
                    'evidence': original[range_match.start():range_match.end()]}
    from_to = re.search(
        r'\bот\s+(\d+)\s*(минут\w*|час\w*)?\s+до\s+(\d+)\s*'
        r'(минут\w*|час\w*)\b', value,
    )
    if from_to:
        low_unit = from_to[2] or from_to[4]
        low = _minutes(from_to[1], low_unit)
        high = _minutes(from_to[3], from_to[4])
        if 5 <= low <= high <= 480:
            return {'type': 'range', 'min_minutes': low, 'max_minutes': high,
                    'evidence': original[from_to.start():from_to.end()]}

    if re.search(r'\bполтора\s+часа?\b', value):
        evidence = re.search(r'\bполтора\s+часа?\b', value).group(0)
        return {'type': 'exact', 'minutes': 90, 'evidence': evidence}
    if re.search(r'\bполчаса\b', value):
        return {'type': 'exact', 'minutes': 30, 'evidence': 'полчаса'}

    approximate_hours = re.search(
        r'\bчаса\s+(\d+|один|два|три|четыре|пять|шесть|семь|восемь)\b',
        original.lower().replace('ё', 'е'),
    )
    if approximate_hours:
        count = int(_numeric(approximate_hours.group(1)))
        if 5 <= count * 60 <= 480:
            return {'type': 'approximate', 'minutes': count * 60,
                    'evidence': approximate_hours.group(0)}

    matches = list(re.finditer(r'\b(\d+)\s*(минут\w*|час\w*)\b', value))
    if matches:
        total = 0
        evidence = []
        for match in matches:
            if re.search(r'\bв\s*$', value[:match.start()]):
                continue
            total += _minutes(match[1], match[2])
            evidence.append(original[match.start():match.end()])
        if 5 <= total <= 480 and evidence:
            approximate = bool(re.search(
                r'\b(?:примерно|около|приблизительно|где-то|ориентировочно)\b',
                value,
            ) or re.search(r'\bчаса\s+\d+\b', value))
            return {'type': 'approximate' if approximate else 'exact',
                    'minutes': total, 'evidence': ' '.join(evidence)}

    bare_hour = re.fullmatch(r'(?:на\s+)?час', value.strip(' .!?'))
    if bare_hour:
        return {'type': 'exact', 'minutes': 60,
                'evidence': original.strip(' .!?')}

    qualitative = (
        ('long', r'\b(?:долго|долгая|продолжительная)\b'),
        ('short', r'\b(?:недолго|короткая|быстро)\b'),
    )
    for label, pattern in qualitative:
        match = re.search(pattern, value)
        if match:
            return {'type': 'qualitative', 'value': label,
                    'evidence': original[match.start():match.end()]}
    return None


def explicit_duration(message):
    parsed = parse_user_duration(message)
    if parsed and parsed['type'] in {'exact', 'approximate'}:
        return parsed['minutes']
    return None


def cancellation_requested(message):
    value = message.lower().replace('ё', 'е')
    return bool(re.match(r'^\s*(?:пожалуйста[, ]+)?(?:отмени(?:ть)?|удали(?:ть)?|отменить|удалить)\b', value))


def duration_request(message):
    value = message.lower().replace('ё', 'е').strip(' .!?')
    if not re.search(r'длит|длил|долго|длительност', value):
        return None
    action = ('update' if re.match(r'^(?:пожалуйста[, ]+)?(?:исправь|измени|поменяй|уточни|обнови)\b', value)
              else 'read' if re.search(r'\b(сколько|какая|какова)\b', value) else None)
    match = re.search(r'\bвстреч[ауыие]\s+с\s+(.+)', value)
    if not action or not match:
        return None
    tail = match.group(1)
    day = None
    date_match = re.search(r'\b(\d{4}-\d{2}-\d{2}|\d{1,2}\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+\d{4})?|послезавтра|завтра|сегодня|вчера)\b', tail)
    if date_match:
        day = date_match.group(1)
        tail = tail[:date_match.start()] + tail[date_match.end():]
    query = re.split(r'\s+(?:будет|будут|длит\w*|длил\w*|на|в)\b', tail, maxsplit=1)[0].strip(' ,')
    if not query:
        return None
    past = bool(re.search(r'\b(длилась|длился|длилось|была|прошлая|прошедшая)\b', value))
    return {'action': action, 'query': 'встреча с ' + query,
            'temporal': 'past' if past else 'future', 'date_expression': day,
            'duration_minutes': explicit_duration(value) if action == 'update' else None,
            'duration_description': 'долго' if action == 'update' and 'долго' in value else None}
