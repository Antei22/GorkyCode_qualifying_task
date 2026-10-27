"""Chat transaction, history, draft state and request idempotency."""
from psycopg.types.json import Jsonb
from fastapi.encoders import jsonable_encoder
from app.assistant.tools import Tools
from app.domain.models import DomainError


class Service:
    def __init__(self, repo, assistant, clock):
        self.repo, self.assistant, self.clock = repo, assistant, clock

    def chat(self, owner, request_id, message):
        with self.repo.connect() as con:
            # A per-session lock keeps concurrent turns and retries consistent.
            session = con.execute('SELECT pending FROM sessions WHERE id=%s FOR UPDATE', (owner,)).fetchone()
            if not session:
                raise DomainError('Сессия не найдена. Обновите страницу.')
            previous = con.execute('SELECT message,response FROM chat_requests WHERE owner_id=%s AND request_id=%s',
                                   (owner, request_id)).fetchone()
            if previous:
                if previous['message'] != message:
                    raise DomainError('Этот request_id уже использован для другого сообщения.')
                return previous['response']
            tools = Tools(self.repo, con, owner, self.clock, session['pending'])
            if message.strip().lower() in ('отмена', 'отмени', 'отменить'):
                tools.save_pending(None)
                answer = 'Черновик отменён.'
            else:
                answer = self.assistant.answer(message, self.repo.history(con, owner), tools)
            # Server-generated confirmations are attached to committed record data.
            if tools.created:
                labels = {'task': 'Задача сохранена', 'meeting': 'Встреча сохранена', 'reminder': 'Напоминание сохранено'}
                answer = '\n'.join(f"{labels[r['kind']]}: «{r['text']}» — "
                    f"{r['scheduled_date'] or 'без даты'} {r['scheduled_time'] or ''}" for r in tools.created)
            elif tools.questions:
                answer = tools.questions[-1]
            elif tools.errors:
                answer = tools.errors[-1]
            elif tools.read_count:
                rows = list(tools.found.values())
                answer = ('Найдены записи:\n' + '\n'.join(
                    f"• {r['text']} — {r['scheduled_date'] or 'без даты'} {r['scheduled_time'] or ''}"
                    + (' (тип в исходных данных не указан)' if r['kind'] is None else '')
                    for r in rows)) if rows else 'По запросу записей не найдено.'
                if len(rows) >= tools.result_limit:
                    answer += '\nВыдача ограничена. Уточните запрос для более точного результата.'
            else:
                # A model-only claim is not evidence that an operation happened.
                answer = ('Я могу создать задачу, встречу или напоминание, показать расписание '
                          'и найти старую запись. Уточните, что нужно сделать.')
            response = jsonable_encoder({'answer': answer,
                'sources': [{'type': r['kind'] or 'import', 'title': str(r['id']),
                             'evidence': f"{r['text']} — {r['scheduled_date']} {r['scheduled_time'] or ''}"}
                            for r in list(tools.found.values())[:20]],
                'records': tools.created or list(tools.found.values()),
                'pending': tools.pending})
            self.repo.remember(con, owner, 'user', message)
            self.repo.remember(con, owner, 'assistant', answer)
            con.execute('INSERT INTO chat_requests(owner_id,request_id,message,response) VALUES (%s,%s,%s,%s)',
                        (owner, request_id, message, Jsonb(response)))
            return response

