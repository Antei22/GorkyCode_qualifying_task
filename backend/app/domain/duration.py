"""Intent and provenance rules for meeting duration, independent of the model."""
import re


def explicit_duration(message):
    value = message.lower()
    if 'полтора часа' in value or 'полутора часов' in value:
        return 90
    if 'полчаса' in value:
        return 30
    numbers = {'один': 1, 'одну': 1, 'два': 2, 'две': 2, 'три': 3,
               'четыре': 4, 'пять': 5, 'шесть': 6, 'семь': 7, 'восемь': 8,
               'девять': 9, 'десять': 10, 'пятнадцать': 15, 'двадцать': 20,
               'тридцать': 30, 'сорок': 40, 'пятьдесят': 50}
    for word, number in numbers.items():
        value = re.sub(rf'\b{word}\b', str(number), value)
    value = re.sub(r'\b(20|30|40|50)\s+([1-9])\b',
                   lambda m: str(int(m[1]) + int(m[2])), value)
    total = 0
    for match in re.finditer(r'\b(\d+)\s*(минут\w*|час\w*)\b', value):
        if re.search(r'\bв\s*$', value[:match.start()]):
            continue
        total += int(match[1]) * (60 if match[2].startswith('час') else 1)
    if total:
        return total
    if re.fullmatch(r'(?:на\s+)?час', value.strip(' .!?')):
        return 60
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
