import { useEffect, useRef, useState } from 'react'
import { ApiError, apiFetch } from './api/client'
import { MessageBubble } from './components/chat/MessageBubble'
import { DateRangePicker, type DateRangeValue } from './components/records/DateRangePicker'
import type { ChatMessage } from './types/chat'
import type { AssistantResponse, RecordItem, Session } from './types/records'
import { findRussianVoice, listRussianVoices, onVoicesChanged, recognitionSupported, setPreferredVoice, speak, startRecognition, stopSpeaking } from './voice/browser'
import type { Recognition } from './voice/browser'

const labels = { task: 'Задача', meeting: 'Встреча', reminder: 'Напоминание' };

function Icon({ name }: { name: 'sparkles' | 'clock' | 'calendar' | 'document' | 'upload' | 'mic' | 'send' | 'search' }) {
  const paths = {
    sparkles: <><path d="M12 3l1.2 3.8L17 8l-3.8 1.2L12 13l-1.2-3.8L7 8l3.8-1.2L12 3Z"/><path d="M5 13l.8 2.2L8 16l-2.2.8L5 19l-.8-2.2L2 16l2.2-.8L5 13Zm13 1 .7 1.8 1.8.7-1.8.7L18 19l-.7-1.8-1.8-.7 1.8-.7L18 14Z"/></>,
    clock: <><circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/></>,
    calendar: <><rect x="4" y="5" width="16" height="15" rx="2"/><path d="M8 3v4m8-4v4M4 10h16"/></>,
    document: <><path d="M7 3h7l4 4v14H7z"/><path d="M14 3v5h5M10 12h5m-5 4h5"/></>,
    upload: <><path d="M7 18H6a4 4 0 0 1 0-8 6 6 0 0 1 11.5-1.5A4 4 0 0 1 18 18h-2"/><path d="M12 19V9m-3 3 3-3 3 3"/></>,
    mic: <><rect x="9" y="3" width="6" height="12" rx="3"/><path d="M6 11a6 6 0 0 0 12 0m-6 6v4m-3 0h6"/></>,
    send: <><path d="m4 5 16 7-16 7 3-7-3-7Z"/><path d="M7 12h13"/></>,
    search: <><circle cx="11" cy="11" r="6"/><path d="m16 16 4 4"/></>,
  };
  return <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">{paths[name]}</svg>;
}

// Единая точка превращения ошибок в понятный пользователю текст (без префиксов «Error:»).
function errorText(reason: unknown): string {
  if (reason instanceof ApiError) return reason.message;
  if (reason instanceof TypeError) return 'Нет связи с сервером. Проверьте сеть и повторите.';
  return reason instanceof Error ? reason.message : String(reason);
}

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [records, setRecords] = useState<RecordItem[]>([]);
  const [input, setInput] = useState('');
  const [dateRange, setDateRange] = useState<DateRangeValue>({ from: null, to: null });
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [listening, setListening] = useState(false);
  const [fromVoice, setFromVoice] = useState(false);
  const [voiceAnswer, setVoiceAnswer] = useState(false);
  const [voices, setVoices] = useState<string[]>([]);
  const [voiceName, setVoiceName] = useState('');
  const recognition = useRef<Recognition | null>(null);
  const lastRequest = useRef<{ message: string; request_id: string } | null>(null);
  const initialized = useRef(false);
  const bottom = useRef<HTMLDivElement>(null);

  async function loadRecords(range: DateRangeValue = dateRange) {
    const params = new URLSearchParams();
    if (range.from && range.to) { params.set('period', 'custom'); params.set('date_from', range.from); params.set('date_to', range.to); }
    else params.set('period', 'all');
    const result = await apiFetch<{ records: RecordItem[] }>('/api/records?' + params.toString());
    setRecords(result.records);
  }

  useEffect(() => {
    // Establish one cookie before parallel requests; avoid competing fresh sessions.
    if (initialized.current) return;
    initialized.current = true;
    async function initialize() {
      try {
        const current = await apiFetch<Session>('/api/session');
        setSession(current);
        const [history, list] = await Promise.all([
          apiFetch<Array<{ role: 'user' | 'assistant'; content: string }>>('/api/history'),
          apiFetch<{ records: RecordItem[] }>('/api/records'),
        ]);
        setMessages(history.map((turn, i) => ({ ...turn, id: `history-${i}`, sources: [] })));
        setRecords(list.records);
        setReady(true);
      } catch (reason) { setError(errorText(reason)); }
    }
    void initialize();
  }, []);

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);
  useEffect(() => () => { recognition.current?.abort(); stopSpeaking(); }, []);
  useEffect(() => {
    // Список голосов в браузере может появиться не сразу, поэтому подписываемся на обновление.
    const refresh = () => {
      setVoices(listRussianVoices().map((voice) => voice.name));
      setVoiceName(findRussianVoice()?.name ?? '');
    };
    refresh();
    return onVoicesChanged(refresh);
  }, []);

  function chooseVoice(name: string) {
    setPreferredVoice(name || null);
    setVoiceName(findRussianVoice()?.name ?? '');
  }

  function usePrompt(value: string) {
    setInput(value);
    setFromVoice(false);
  }

  async function send() {
    const message = input.trim();
    if (!message || busy || !ready) return;
    // abort(), а не stop(): после abort поздняя расшифровка не придёт и не перезапишет поле.
    recognition.current?.abort();
    setListening(false);
    stopSpeaking();
    setBusy(true); setError(''); setNotice('');
    const request = lastRequest.current?.message === message ? lastRequest.current :
      { message, request_id: crypto.randomUUID() };
    lastRequest.current = request;
    setMessages((previous) => previous.some((turn) => turn.id === request.request_id) ? previous :
      [...previous, { id: request.request_id, role: 'user', content: message, sources: [] }]);
    try {
      const result = await apiFetch<AssistantResponse>('/api/chat', { method: 'POST', json: request });
      setMessages((previous) => [...previous, { id: `${request.request_id}-answer`, role: 'assistant',
        content: result.answer, sources: result.sources }]);
      setInput(''); setFromVoice(false);
      lastRequest.current = null;
      if (voiceAnswer) speak(result.answer, undefined, setNotice);
      // Reload uses its own error: a refresh failure must not encourage duplicate creation.
      try { await loadRecords(); } catch (reason) { setError(`Ответ получен, но список не обновился: ${errorText(reason)}`); }
    } catch (reason) { setError(`${errorText(reason)} Повторная отправка того же текста использует тот же ключ запроса.`); }
    finally { setBusy(false); }
  }

  function toggleRecording() {
    if (listening) { recognition.current?.stop(); return; }
    setError(''); setNotice('');
    const base = input.trim();
    try {
      recognition.current = startRecognition(
        (text) => { setInput(base ? `${base} ${text}` : text); setFromVoice(true); },
        setError,
        () => setListening(false),
      );
      setListening(true);
    } catch (reason) { setError(errorText(reason)); }
  }

  async function changeDateRange(next: DateRangeValue) {
    setDateRange(next); setError('');
    try { await loadRecords(next); } catch (reason) { setError(errorText(reason)); }
  }

  const [statusBusyId, setStatusBusyId] = useState<string | null>(null);
  async function toggleRecordStatus(record: RecordItem) {
    if (statusBusyId) return;
    setStatusBusyId(record.id); setError('');
    try {
      const result = await apiFetch<{ record: RecordItem }>(`/api/records/${record.id}/status`, {
        method: 'PATCH', json: { status: record.status === 'done' ? 'open' : 'done', expected_version: Number(record.version) },
      });
      setRecords((previous) => previous.map((item) => item.id === record.id ? result.record : item));
    } catch (reason) {
      setError(errorText(reason));
      if (reason instanceof ApiError && reason.status === 409) { try { await loadRecords(); } catch { /* primary error shown */ } }
    } finally { setStatusBusyId(null); }
  }

  async function importDemo() {
    setBusy(true); setError(''); setNotice('');
    try {
      const report = await apiFetch<{ imported: number; skipped: number }>('/api/import/demo', { method: 'POST' });
      setNotice(`Импортировано: ${report.imported}; уже были загружены: ${report.skipped}.`);
      await loadRecords();
    } catch (reason) { setError(errorText(reason)); }
    finally { setBusy(false); }
  }

  return (
    <main className="app-shell">
      <header className="hero">
        <div className="hero__copy">
          <p className="eyebrow">GorkyCode</p>
          <h1>Задачи, встречи и напоминания</h1>
          <p className="hero__subtitle">Планируйте дела, встречи и напоминания в одном месте.</p>
        </div>
        <div className="hero__art" aria-hidden="true"><span>31</span></div>
      </header>

      <div className="status-stack">
        {session && !session.llm_configured && <p className="notice">Для чата настройте LLM в .env. Импорт и поиск уже доступны.</p>}
        {error && <p className="error" role="alert">{error}</p>}
        {notice && <p className="notice" role="status">{notice}</p>}
      </div>

      <div className="workspace">
        <section className="workspace-panel assistant-panel" aria-label="Диалог">
          <div className="panel-heading">
            <span className="heading-icon"><Icon name="sparkles" /></span>
            <div><h2>Ассистент</h2><p>Поможет найти записи, запланировать задачи и напоминания.</p></div>
          </div>

          <div className="quick-actions" aria-label="Примеры запросов">
            <button type="button" onClick={() => usePrompt('Напомни завтра в 10 позвонить Ивану')}><Icon name="clock" /><span>Напомни завтра<br />в 10 позвонить Ивану</span></button>
            <button type="button" onClick={() => usePrompt('Что у меня на этой неделе?')}><Icon name="calendar" /><span>Что у меня<br />на этой неделе?</span></button>
            <button type="button" onClick={() => usePrompt('Найди записи про договор')}><Icon name="document" /><span>Найди записи<br />про договор</span></button>
          </div>

          <div className="messages" aria-live="polite">
            {!messages.length && <div className="empty-chat"><Icon name="sparkles" /><p>Начните диалог — напишите, что нужно запланировать или найти.</p></div>}
            {messages.map((message) => <MessageBubble key={message.id} message={message} />)}
            {busy && <p className="processing">Ассистент думает…</p>}<div ref={bottom} />
          </div>

          <form className="composer" onSubmit={(event) => { event.preventDefault(); void send(); }}>
            <div className="composer__input">
              <textarea id="message" aria-label="Запрос или расшифровка голоса" value={input}
                onChange={(event) => { setInput(event.target.value); setFromVoice(false); }} maxLength={4000}
                placeholder="Напишите запрос или задайте вопрос..." disabled={busy} readOnly={listening} />
              <button type="button" className={`round-button mic-button${listening ? ' is-listening' : ''}`}
                onClick={toggleRecording} aria-label={listening ? 'Остановить запись' : 'Включить микрофон'}
                aria-pressed={listening} disabled={busy || !ready || !recognitionSupported()}><Icon name="mic" /></button>
              <button type="submit" className="round-button send-button" aria-label="Отправить"
                disabled={busy || !ready || listening || !input.trim()}><Icon name="send" /></button>
            </div>
            {listening && <p className="recording" role="status">Идёт запись — говорите.</p>}
            {!listening && fromVoice && input.trim() && <p className="hint">Проверьте распознанный текст перед отправкой.</p>}
            <div className="voice-controls">
              <label htmlFor="voice">Голос озвучки</label>
              <select id="voice" value={voiceName} onChange={(event) => chooseVoice(event.target.value)} disabled={voices.length < 2}>
                {!voices.length && <option>Системный голос</option>}
                {voices.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
              <label className="switch"><input type="checkbox" checked={voiceAnswer} onChange={(event) => setVoiceAnswer(event.target.checked)} /><span aria-hidden="true" />Озвучивать ответы</label>
              <button type="button" className="stop-voice" onClick={stopSpeaking}>Стоп</button>
            </div>
          </form>
        </section>

        <section className="workspace-panel records-panel" aria-label="Сохранённые записи">
          <div className="records-heading">
            <div className="panel-heading"><span className="heading-icon"><Icon name="document" /></span>
              <div><h2>Сохранённые записи</h2><p>Просматривайте и находите все ваши задачи, встречи и заметки.</p></div>
            </div>
            <button className="dataset-button" disabled={busy || !ready} onClick={() => void importDemo()}><Icon name="upload" />Загрузить датасет</button>
          </div>

          <div className="filters filters--calendar-only">
            <div className="filter-field"><label>Период</label><DateRangePicker value={dateRange} disabled={!ready || busy} onChange={(next) => void changeDateRange(next)} /></div>
          </div>

          <div className="records" tabIndex={0} aria-label="Список сохранённых записей">
            {!records.length && <div className="empty-records"><Icon name="document" /><p>Записей пока нет.<br />Создайте их в чате или загрузите датасет.</p></div>}
            {records.map((record) => <article className={`record record--${record.kind}${record.status === 'done' ? ' record--done' : ''}`} key={record.id}>
              <button type="button" className={`record-check${record.status === 'done' ? ' is-done' : ''}`} aria-label={record.status === 'done' ? `Вернуть в невыполненные: ${record.text}` : `Отметить выполненным: ${record.text}`} aria-pressed={record.status === 'done'} disabled={statusBusyId === record.id || record.status === 'cancelled'} onClick={() => void toggleRecordStatus(record)}>{record.status === 'done' ? '✓' : ''}</button>
              <div className="record-main"><span className="badge">{labels[record.kind]}</span><h3>{record.text}</h3>
                <small>{record.timezone} · {record.id.slice(0, 8)}</small></div>
              <time>{record.scheduled_date ? new Date(`${record.scheduled_date}T00:00:00`).toLocaleDateString('ru-RU', { day: '2-digit', month: 'short', year: 'numeric' }) : 'Без даты'}{record.scheduled_time ? `, ${record.scheduled_time.slice(0, 5)}` : ''}</time>
              <button type="button" className="record-menu" aria-label={`Действия: ${record.text}`}>•••</button>
            </article>)}
          </div>
          <p className="records-footer">Показано записей: {records.length}</p>
        </section>
      </div>
    </main>
  );
}
