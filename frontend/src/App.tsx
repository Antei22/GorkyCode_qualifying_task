import { useEffect, useRef, useState } from 'react'
import { apiFetch } from './api/client'
import { MessageBubble } from './components/chat/MessageBubble'
<<<<<<< Updated upstream
=======
import { DateRangePicker, type DateRangeValue } from './components/records/DateRangePicker'
import { RecordCard } from './components/records/RecordCard'
>>>>>>> Stashed changes
import type { ChatMessage } from './types/chat'
import type { AssistantResponse, RecordItem, Session } from './types/records'
import { recognitionSupported, speak, startRecognition, stopSpeaking } from './voice/browser'
import type { Recognition } from './voice/browser'

<<<<<<< Updated upstream
const labels = { task: 'Задача', meeting: 'Встреча', reminder: 'Напоминание' };
=======
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
>>>>>>> Stashed changes

export default function App() {
  const [session, setSession] = useState<Session | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [records, setRecords] = useState<RecordItem[]>([]);
  const [input, setInput] = useState('');
  const [query, setQuery] = useState('');
  const [period, setPeriod] = useState('all');
  const [busy, setBusy] = useState(false);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [listening, setListening] = useState(false);
  const [voiceAnswer, setVoiceAnswer] = useState(false);
  const recognition = useRef<Recognition | null>(null);
  const lastRequest = useRef<{ message: string; request_id: string } | null>(null);
  const initialized = useRef(false);
  const bottom = useRef<HTMLDivElement>(null);

  async function loadRecords(nextPeriod = period, nextQuery = query) {
    const params = new URLSearchParams({ period: nextQuery.trim() ? 'all' : nextPeriod, query: nextQuery.trim() });
    const result = await apiFetch<{ records: RecordItem[] }>('/api/records?' + params);
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
      } catch (reason) { setError(String(reason)); }
    }
    void initialize();
  }, []);

  useEffect(() => { bottom.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);
  useEffect(() => () => { recognition.current?.abort(); stopSpeaking(); }, []);

  async function send() {
    const message = input.trim();
    if (!message || busy || !ready) return;
    recognition.current?.stop();
    setBusy(true); setError('');
    const request = lastRequest.current?.message === message ? lastRequest.current :
      { message, request_id: crypto.randomUUID() };
    lastRequest.current = request;
    setMessages((previous) => previous.some((turn) => turn.id === request.request_id) ? previous :
      [...previous, { id: request.request_id, role: 'user', content: message, sources: [] }]);
    try {
      const result = await apiFetch<AssistantResponse>('/api/chat', { method: 'POST', json: request });
      setMessages((previous) => [...previous, { id: `${request.request_id}-answer`, role: 'assistant',
        content: result.answer, sources: result.sources }]);
      setInput(''); lastRequest.current = null;
      if (voiceAnswer) speak(result.answer);
      // Reload uses its own error: a refresh failure must not encourage duplicate creation.
      try { await loadRecords(); } catch (reason) { setError(`Ответ получен, но список не обновился: ${String(reason)}`); }
    } catch (reason) { setError(`${String(reason)} Повторная отправка того же текста использует тот же ключ запроса.`); }
    finally { setBusy(false); }
  }

  function toggleRecording() {
    if (listening) { recognition.current?.stop(); return; }
    setError('');
    try {
      recognition.current = startRecognition(setInput, setError, () => setListening(false));
      setListening(true);
    } catch (reason) { setError(String(reason)); }
  }

  async function importDemo() {
    setBusy(true); setError(''); setNotice('');
    try {
      const report = await apiFetch<{ imported: number; skipped: number }>('/api/import/demo', { method: 'POST' });
      setNotice(`Импортировано: ${report.imported}; уже были загружены: ${report.skipped}.`);
      await loadRecords();
    } catch (reason) { setError(String(reason)); }
    finally { setBusy(false); }
  }

  return (
    <main>
      <header><div><p className="eyebrow">GorkyCode</p><h1>Задачи, встречи и напоминания</h1>
        <p>Скажите или напишите, что запланировать.</p></div>
        <button disabled={busy || !ready} onClick={() => void importDemo()}>Загрузить 1000 примеров</button>
      </header>
      {session && <p className="session">{session.timezone} · {new Date(session.now).toLocaleString('ru-RU', { timeZone: session.timezone })}
        {session.fixed_clock ? ' · фиксированное время демо' : ' · время открытия страницы'}<br />Сессия: <code>{session.id}</code></p>}
      {session && !session.llm_configured && <p className="notice">Для чата настройте LLM в .env. Импорт, поиск и список уже доступны.</p>}
      {error && <p className="error" role="alert">{error}</p>}
      {notice && <p className="notice" role="status">{notice}</p>}
      <div className="layout">
        <section className="panel chat" aria-label="Диалог">
          <h2>Ассистент</h2>
          <div className="messages" aria-live="polite">
            {!messages.length && <p className="muted">«Напомни завтра в 10 позвонить Ивану»<br />«Что у меня на этой неделе?»<br />«Найди записи про договор»</p>}
            {messages.map((message) => <MessageBubble key={message.id} message={message} />)}
            {busy && <p className="muted">Обработка…</p>}<div ref={bottom} />
          </div>
          <form onSubmit={(event) => { event.preventDefault(); void send(); }}>
            <label htmlFor="message">Запрос или расшифровка голоса</label>
            <textarea id="message" value={input} onChange={(event) => setInput(event.target.value)}
              maxLength={4000} placeholder="Введите запрос…" disabled={busy} />
            <div className="controls">
              <button type="button" onClick={toggleRecording} disabled={busy || !ready || !recognitionSupported()}>
                {listening ? 'Остановить запись' : 'Микрофон'}</button>
              <button type="submit" className="primary" disabled={busy || !ready || !input.trim()}>Отправить</button>
            </div>
            <label className="checkbox"><input type="checkbox" checked={voiceAnswer}
              onChange={(event) => setVoiceAnswer(event.target.checked)} />Озвучивать ответы</label>
            <button type="button" className="text-button" onClick={stopSpeaking}>Остановить озвучку</button>
            <p className="hint">Расшифровку можно исправить перед отправкой. Распознавание зависит от браузера и сети.
              {!recognitionSupported() && ' В этом браузере доступен только текстовый ввод.'}</p>
          </form>
        </section>
<<<<<<< Updated upstream
        <section className="panel" aria-label="Сохранённые записи">
          <h2>Сохранённые записи</h2>
          <form className="filters" onSubmit={(event) => { event.preventDefault(); setError(''); void loadRecords().catch((reason) => setError(String(reason))); }}>
            <label htmlFor="period">Период</label>
            <select id="period" value={period} onChange={(event) => setPeriod(event.target.value)}>
              <option value="all">Все даты</option><option value="today">Сегодня</option>
              <option value="tomorrow">Завтра</option><option value="this_week">Эта неделя</option>
              <option value="next_week">Следующая неделя</option>
            </select>
            <label htmlFor="search">Поиск по всей истории</label>
            <input id="search" value={query} onChange={(event) => setQuery(event.target.value)} maxLength={300} placeholder="Например, договор" />
            <button disabled={!ready || busy}>Показать</button>
          </form>
          <p className="hint">До 100 записей в списке, до 20 в поиске. У импорта тип не указан.
            Фоновая доставка напоминаний пока не реализована.</p>
          <div className="records">
            {!records.length && <p className="muted">Записей нет. Создайте запись в чате или загрузите примеры.</p>}
            {records.map((record) => <article className="record" key={record.id}>
              <span className="badge">{record.kind ? labels[record.kind] : 'Импорт · тип неизвестен'}</span>
              <h3>{record.text}</h3><p>{record.scheduled_date ?? 'Без даты'} {record.scheduled_time?.slice(0, 5) ?? ''}</p>
              <small>{record.timezone} · {record.id.slice(0, 8)}</small>
            </article>)}
=======

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
            {records.map((record) => <RecordCard key={record.id} record={record}
              busy={statusBusyId === record.id} onToggle={(item) => void toggleRecordStatus(item)} />)}
>>>>>>> Stashed changes
          </div>
        </section>
      </div>
    </main>
  );
}
