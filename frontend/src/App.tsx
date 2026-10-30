import { useEffect, useRef, useState } from 'react'
import { ApiError, apiFetch } from './api/client'
import { MessageBubble } from './components/chat/MessageBubble'
import type { ChatMessage } from './types/chat'
import type { AssistantResponse, RecordItem, Session } from './types/records'
import { findRussianVoice, listRussianVoices, onVoicesChanged, recognitionSupported, setPreferredVoice, speak, startRecognition, stopSpeaking } from './voice/browser'
import type { Recognition } from './voice/browser'

const labels = { task: 'Задача', meeting: 'Встреча', reminder: 'Напоминание' };

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
  const [query, setQuery] = useState('');
  const [period, setPeriod] = useState('all');
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
    <main>
      <header><div><p className="eyebrow">GorkyCode</p><h1>Задачи, встречи и напоминания</h1>
        <p>Скажите или напишите, что запланировать.</p></div>
        <button disabled={busy || !ready} onClick={() => void importDemo()}>Загрузить 1000 примеров</button>
      </header>
      {session && <p className="session">{session.timezone} · {new Date(session.now).toLocaleString('ru-RU', { timeZone: session.timezone })}
        {session.fixed_clock ? ' · фиксированное время демо' : ' · время открытия страницы'}<br />Сессия: <code>{session.id}</code><br />
        История и записи привязаны к этому браузеру. В другом браузере или после очистки cookie откроется пустая сессия.</p>}
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
            <textarea id="message" value={input} onChange={(event) => { setInput(event.target.value); setFromVoice(false); }}
              maxLength={4000} placeholder="Введите запрос…" disabled={busy} readOnly={listening} />
            {listening && <p className="recording" role="status">Идёт запись. Говорите: она остановится сама после паузы.</p>}
            {!listening && fromVoice && input.trim() && <p className="hint" role="status">
              Текст получен с микрофона. Проверьте его, исправьте при необходимости и нажмите «Отправить».</p>}
            <div className="controls">
              <button type="button" onClick={toggleRecording} aria-pressed={listening}
                disabled={busy || !ready || !recognitionSupported()}>
                {listening ? 'Остановить запись' : 'Микрофон'}</button>
              <button type="submit" className="primary" disabled={busy || !ready || listening || !input.trim()}>Отправить</button>
            </div>
            <label className="checkbox"><input type="checkbox" checked={voiceAnswer}
              onChange={(event) => setVoiceAnswer(event.target.checked)} />Озвучивать ответы</label>
            <button type="button" className="text-button" onClick={stopSpeaking}>Остановить озвучку</button>
            {voices.length > 1 && <>
              <label htmlFor="voice">Голос озвучки</label>
              <select id="voice" value={voiceName} onChange={(event) => chooseVoice(event.target.value)}>
                {voices.map((name) => <option key={name} value={name}>{name}</option>)}
              </select>
            </>}
            <button type="button" className="text-button"
              onClick={() => speak('Здравствуйте! Так звучит выбранный голос.', undefined, setNotice)}>Проверить голос</button>
            <p className="hint">Расшифровку можно исправить перед отправкой. Распознавание зависит от браузера и сети:
              в Chrome и Edge аудио обрабатывает сервис браузера.
              {!recognitionSupported() && ' В этом браузере доступен только текстовый ввод.'}</p>
          </form>
        </section>
        <section className="panel" aria-label="Сохранённые записи">
          <h2>Сохранённые записи</h2>
          <form className="filters" onSubmit={(event) => { event.preventDefault(); setError(''); void loadRecords().catch((reason) => setError(errorText(reason))); }}>
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
          </div>
        </section>
      </div>
    </main>
  );
}
