"""Chat transaction, history, draft state and request idempotency."""
import re
from psycopg.types.json import Jsonb
from fastapi.encoders import jsonable_encoder
from app.assistant.tools import Tools
from app.domain.models import DomainError
from app.domain.duration import duration_request, explicit_duration
from app.application.operations import (
    DefaultChangePlanner, DefaultOutcomePresenter, OperationCoordinator,
)
from app.application.operations.calendar_policy import CalendarOperationPolicy
from app.application.operations.codec import dump_pending, load_pending
from app.application.operations.ports import TurnContext
from app.storage.operation_adapters import (
    PostgresOperationExecutor, PostgresTargetResolver,
)
from app.observability import pipeline_event


class Service:
    def __init__(self, repo, assistant, clock, operation_interpreter=None):
        self.repo, self.assistant, self.clock = repo, assistant, clock
        self.operation_interpreter = operation_interpreter
        self.operation_presenter = DefaultOutcomePresenter()

    @staticmethod
    def _ambiguous_call(message):
        """Return a deterministic draft for an intent that may be a meeting or reminder."""
        value = message.strip()
        normalized = value.lower().replace('ё', 'е')
        if re.search(r'\b(встреч\w*|напом\w*|задач\w*)\b', normalized):
            return None
        if not re.search(r'\b(хочу|планирую|собираюсь|нужно|надо)\b', normalized):
            return None
        if not re.search(r'\b(созвон\w*|позвон\w*)\b', normalized):
            return None

        reminder_text = re.sub(
            r'^\s*(?:я\s+)?(?:хочу|планирую|собираюсь|нужно|надо)\s+', '', value,
            flags=re.IGNORECASE,
        ).strip(' .,!')
        meeting_text = reminder_text
        date_expression = None
        date_match = re.search(r'\b(послезавтра|завтра|сегодня)\b', meeting_text,
                               flags=re.IGNORECASE)
        if date_match:
            date_expression = date_match.group(1)
            meeting_text = (meeting_text[:date_match.start()] + meeting_text[date_match.end():]).strip()

        time_expression = None
        time_match = re.search(
            r'\bв\s+(\d{1,2})(?::(\d{2}))?\s*(?:час(?:а|ов)?\b)?',
            meeting_text, flags=re.IGNORECASE,
        )
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2) or 0)
            # In conversational requests «в 4 часа» normally means daytime;
            # explicit «утра/вечера» is intentionally left to the regular parser.
            if 1 <= hour <= 7:
                hour += 12
            time_expression = f'{hour:02d}:{minute:02d}'
            meeting_text = (meeting_text[:time_match.start()] + meeting_text[time_match.end():]).strip()

        meeting_text = re.sub(r'\s+', ' ', meeting_text).strip(' .,!')
        return {
            'reminder_text': reminder_text,
            'meeting_text': meeting_text or reminder_text,
            'date_expression': date_expression,
            'time_expression': time_expression,
        }

    def chat(self, owner, request_id, message):
        try:
            if self.operation_interpreter is None:
                pipeline_event(
                    'service', 'route_selected', request_id=str(request_id),
                    owner_id=str(owner), route='legacy', reason='unified_disabled',
                )
                return self._legacy_chat(owner, request_id, message)
            # Let an already-started legacy draft finish through its original state
            # machine. Every new operation uses the unified path.
            with self.repo.connect() as con:
                state = con.execute(
                    'SELECT pending,operation_state FROM sessions WHERE id=%s', (owner,)
                ).fetchone()
            if state and state['pending'] and not state['operation_state']:
                pipeline_event(
                    'service', 'route_selected', request_id=str(request_id),
                    owner_id=str(owner), route='legacy', reason='legacy_draft_exists',
                )
                return self._legacy_chat(owner, request_id, message)
            pipeline_event(
                'service', 'route_selected', request_id=str(request_id),
                owner_id=str(owner), route='unified', reason='new_operation',
            )
            return self._operation_chat(owner, request_id, message)
        except Exception as error:
            pipeline_event(
                'service', 'request_failed', request_id=str(request_id),
                owner_id=str(owner), error_type=type(error).__name__,
                error=str(error),
            )
            raise

    def _operation_chat(self, owner, request_id, message):
        with self.repo.connect() as con:
            session = con.execute(
                'SELECT operation_state FROM sessions WHERE id=%s FOR UPDATE', (owner,)
            ).fetchone()
            pipeline_event(
                'transaction', 'session_locked', request_id=str(request_id),
                owner_id=str(owner), lock='sessions FOR UPDATE',
            )
            if not session:
                raise DomainError('Сессия не найдена. Обновите страницу.')
            previous = con.execute(
                'SELECT message,response FROM chat_requests '
                'WHERE owner_id=%s AND request_id=%s', (owner, request_id)
            ).fetchone()
            if previous:
                if previous['message'] != message:
                    raise DomainError('Этот request_id уже использован для другого сообщения.')
                pipeline_event(
                    'transaction', 'idempotent_replay', request_id=str(request_id),
                    owner_id=str(owner), source='chat_requests',
                )
                return previous['response']
            pending = (load_pending(session['operation_state'])
                       if session['operation_state'] else None)
            history_rows = self.repo.history(con, owner)
            context = TurnContext(
                owner_id=owner, request_id=request_id, message=message,
                now=self.clock.now(),
                history=tuple((row['role'], row['content']) for row in history_rows),
            )
            pipeline_event(
                'service', 'context_built', context,
                now=context.now, history_messages=len(context.history),
                pending_phase=pending.phase if pending else None,
                pending_operation_id=pending.operation_id if pending else None,
            )
            resolver = PostgresTargetResolver(self.repo, con)
            planner = DefaultChangePlanner()
            policy = CalendarOperationPolicy(self.repo, con)
            executor = PostgresOperationExecutor(self.repo, con)
            coordinator = OperationCoordinator(
                interpreter=self.operation_interpreter,
                resolver=resolver, planner=planner, policy=policy, executor=executor,
            )
            outcome = coordinator.handle(context, pending)
            pipeline_event(
                'coordinator', 'outcome_received', context,
                phase=outcome.phase, records_count=len(outcome.records),
                issue_codes=[item.code for item in outcome.issues],
                has_pending=outcome.pending is not None,
                executed=outcome.execution is not None,
            )
            state_payload = dump_pending(outcome.pending) if outcome.pending else None
            con.execute(
                'UPDATE sessions SET operation_state=%s WHERE id=%s',
                (Jsonb(state_payload) if state_payload else None, owner),
            )
            answer = self.operation_presenter.present(context, outcome)
            pipeline_event(
                'presenter', 'answer_rendered', context,
                answer=answer,
            )
            records = [self._snapshot_payload(item) for item in outcome.records]
            response = jsonable_encoder({
                'answer': answer,
                'sources': [
                    {'type': item.kind or 'import', 'title': str(item.id),
                     'evidence': f'{item.text} — {item.scheduled_date} {item.scheduled_time or ""}'}
                    for item in outcome.records[:20]
                ],
                'records': records,
                'pending': state_payload,
            })
            self.repo.remember(con, owner, 'user', message)
            self.repo.remember(con, owner, 'assistant', answer)
            con.execute(
                'INSERT INTO chat_requests(owner_id,request_id,message,response) '
                'VALUES (%s,%s,%s,%s)',
                (owner, request_id, message, Jsonb(response)),
            )
            pipeline_event(
                'transaction', 'changes_staged', context,
                operation_state_saved=state_payload is not None,
                history_saved=True, idempotency_saved=True,
                note='commit occurs when the transaction context exits',
            )
            return response

    @staticmethod
    def _snapshot_payload(item):
        return {
            'id': item.id, 'kind': item.kind, 'text': item.text,
            'scheduled_date': item.scheduled_date,
            'scheduled_time': item.scheduled_time,
            'duration_minutes': item.duration.minutes,
            'duration_source': item.duration.source,
            'duration_description': item.duration.description,
            'timezone': item.timezone, 'status': item.status,
            'version': item.revision,
        }

    def _legacy_chat(self, owner, request_id, message):
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
            tools.user_message = message
            cancelled = False
            normalized_message = message.strip().lower().replace('ё', 'е').strip(' !.,?')
            duration_intent = duration_request(message)

            cancel_phrases = {
                'отмена',
                'отмени',
                'отменить',
                'отмени создание',
                'отмени черновик',
            }
            cancel_record_match = re.fullmatch(
                r'(?:отмени|удали)\s+(встречу|задачу|напоминание)\s+(.+)',
                normalized_message,
            )

            if duration_intent:
                arguments = {'query': duration_intent['query'],
                             'date_expression': duration_intent['date_expression']}
                if duration_intent['action'] == 'read':
                    tools.call('get_meeting_duration', {
                        **arguments, 'temporal': duration_intent['temporal'],
                    })
                else:
                    tools.call('update_meeting_duration', {
                        **arguments,
                        'duration_minutes': duration_intent['duration_minutes'],
                        'duration_description': duration_intent['duration_description'],
                    })
                answer = ''
            elif ((tools.pending or {}).get('_duration_update')
                  and (explicit_duration(message) is not None or
                       re.search(r'не знаю|неизвестно|долго|не уверен', normalized_message))):
                tools.call('update_meeting_duration', {
                    'mode': 'continue', 'duration_minutes': explicit_duration(message),
                    'duration_description': message if explicit_duration(message) is None else None,
                })
                answer = ''
            elif cancel_record_match:
                kind = {
                    'встречу': 'meeting',
                    'задачу': 'task',
                    'напоминание': 'reminder',
                }[cancel_record_match.group(1)]
                query = re.sub(r'^с\s+', '', cancel_record_match.group(2)).strip()
                tools.call('cancel_record', {'query': query, 'kind': kind})
                answer = ''
            elif normalized_message in cancel_phrases:
                pending = tools.pending or {}
                kind_labels = {
                    'task': 'задачи',
                    'meeting': 'встречи',
                    'reminder': 'напоминания',
                }
                if pending:
                    kind_label = kind_labels.get(pending.get('kind'), 'записи')
                    cancel_answer = f'Черновик создания {kind_label} отменён.'
                else:
                    cancel_answer = 'Незавершённого черновика нет.'
                tools.save_pending(None)
                cancelled = True
                answer = cancel_answer
            elif ((tools.pending or {}).get('_suggested_slot') and
                  normalized_message in {
                      'да', 'давай', 'подтверждаю', 'подтвердить', 'подходит',
                      'согласен', 'согласна', 'хорошо', 'ок', 'окей',
                  }):
                suggestion = tools.pending['_suggested_slot']
                tools.call('create_record', {
                    'mode': 'continue',
                    'date_expression': suggestion['date_expression'],
                    'time_expression': suggestion['time_expression'],
                })
                answer = ''
            elif ((tools.pending or {}).get('_suggested_slot') and
                  normalized_message in {
                      'нет', 'не надо', 'не подходит', 'другое время',
                      'не согласен', 'не согласна',
                  }):
                pending = dict(tools.pending)
                pending.pop('_suggested_slot', None)
                tools.save_pending(pending)
                answer = 'Хорошо. Укажите другое время для встречи.'
            elif (tools.pending and tools.pending.get('kind') == 'meeting'
                  and tools.pending.get('duration_minutes') is None
                  and (normalized_message in {
                      'не знаю', 'неизвестно', 'без понятия', 'не уверен', 'не уверена',
                      'неважно', 'не важно', 'как обычно', 'на твое усмотрение',
                      'на ваше усмотрение', 'не могу сказать',
                  } or re.search(r'не знаю|неизвестно|долго|не уверен', normalized_message))):
                tools.call('create_record', {
                    'mode': 'continue',
                    'duration_unspecified': True,
                })
                answer = ''
            elif ((tools.pending or {}).get('_ambiguous_call') and
                  normalized_message in {
                      'встреча', 'встречу', 'это встреча', 'создай встречу',
                      'напоминание', 'это напоминание', 'создай напоминание',
                  }):
                pending = dict(tools.pending)
                intent = pending.pop('_ambiguous_call')
                is_meeting = 'встреч' in normalized_message
                pending['text'] = (intent['meeting_text'] if is_meeting
                                   else intent['reminder_text'])
                if is_meeting:
                    if intent.get('date_expression'):
                        pending['date_expression'] = intent['date_expression']
                    if intent.get('time_expression'):
                        pending['time_expression'] = intent['time_expression']
                else:
                    pending.pop('date_expression', None)
                    pending.pop('time_expression', None)
                tools.save_pending(pending)
                tools.call('create_record', {
                    'mode': 'continue',
                    'kind': 'meeting' if is_meeting else 'reminder',
                })
                answer = ''
            elif not tools.pending and (ambiguous := self._ambiguous_call(message)):
                tools.call('create_record', {
                    'mode': 'start',
                    'text': ambiguous['reminder_text'],
                })
                tools.questions[-1] = 'Что создать: встречу или напоминание?'
                pending = dict(tools.pending or {})
                pending['_ambiguous_call'] = ambiguous
                tools.save_pending(pending)
                answer = ''
            else:
                answer = self.assistant.answer(
                    message,
                    self.repo.history(con, owner),
                    tools,
                )
            # Server-generated confirmations are attached to committed record data.
            if cancelled:
                answer = cancel_answer
            elif tools.created:
                labels = {'task': 'Задача сохранена',
                          'meeting': 'Встреча сохранена',
                          'reminder': 'Напоминание сохранено'
                }
                answer = '\n'.join(f"{labels[r['kind']]}: «{r['text']}» — "
                    f"{r['scheduled_date'] or 'без даты'} {r['scheduled_time'] or ''}"
                    for r in tools.created)
            elif tools.cancelled:
                answer = '\n'.join(
                    f"Запись отменена: «{r['text']}» — {r['scheduled_date'] or 'без даты'} {r['scheduled_time'] or ''}"
                    for r in tools.cancelled)
            elif tools.updated:
                answer = '\n'.join(
                    f"Длительность встречи «{r['text']}» обновлена. "
                    + (f"Указано {r['duration_minutes']} мин."
                       if r['duration_source'] == 'user'
                       else 'Точное время неизвестно; сохранено описание и обновлена оценка для расписания.')
                    for r in tools.updated)
                if tools.answers:
                    answer += '\n' + '\n'.join(tools.answers)
            elif tools.answers:
                answer = '\n'.join(tools.answers)
            elif tools.questions:
                if tools.response_mode != 'clarification' or not answer or not answer.strip():
                    answer = tools.questions[-1]
            elif tools.errors:
                answer = tools.errors[-1]
            elif tools.read_count:
                rows = list(tools.found.values())
                if tools.response_mode != 'read' or not answer or not answer.strip():
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
                'records': tools.created or tools.cancelled or tools.updated or list(tools.found.values()),
                'pending': tools.pending})
            self.repo.remember(con, owner, 'user', message)
            self.repo.remember(con, owner, 'assistant', answer)
            con.execute('INSERT INTO chat_requests(owner_id,request_id,message,response) VALUES (%s,%s,%s,%s)',
                        (owner, request_id, message, Jsonb(response)))
            return response

