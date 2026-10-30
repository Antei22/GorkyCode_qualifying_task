// Browser voice adapter. Business logic receives only editable text.
interface RecognitionResult {
  isFinal: boolean;
  0: { transcript: string };
}
interface RecognitionEvent {
  results: ArrayLike<RecognitionResult>;
}
export interface Recognition {
  lang: string;
  interimResults: boolean;
  continuous: boolean;
  onresult: ((event: RecognitionEvent) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  onend: (() => void) | null;
  start(): void;
  stop(): void;
  abort(): void;
}
type VoiceWindow = Window & {
  SpeechRecognition?: new () => Recognition;
  webkitSpeechRecognition?: new () => Recognition;
};

const ERROR_MESSAGES: Record<string, string> = {
  'not-allowed': 'Доступ к микрофону запрещён. Разрешите его в настройках браузера или введите запрос текстом.',
  'service-not-allowed': 'Браузер не разрешает распознавание речи на этой странице. Введите запрос текстом.',
  'audio-capture': 'Микрофон не найден или занят другим приложением. Можно ввести запрос текстом.',
  'no-speech': 'Речь не распознана. Попробуйте ещё раз или введите запрос текстом.',
  network: 'Нет связи с сервисом распознавания речи. Введите запрос текстом.',
  'language-not-supported': 'Русский язык распознавания недоступен в этом браузере. Введите запрос текстом.',
};

function describeError(code: string): string {
  return ERROR_MESSAGES[code] ?? `Ошибка распознавания: ${code}. Можно ввести запрос текстом.`;
}

export function recognitionSupported(): boolean {
  const target = window as VoiceWindow;
  return Boolean(target.SpeechRecognition ?? target.webkitSpeechRecognition);
}

/**
 * onText получает весь распознанный текст и флаг isFinal.
 * Пока isFinal === false, текст только показывается как подсказка;
 * отправка в чат возможна только после явного действия пользователя.
 * Старые вызовы с одним аргументом продолжают работать.
 */
export function startRecognition(
  onText: (text: string, isFinal: boolean) => void,
  onError: (message: string) => void,
  onEnd: () => void,
): Recognition {
  const target = window as VoiceWindow;
  const Constructor = target.SpeechRecognition ?? target.webkitSpeechRecognition;
  if (!Constructor) throw new Error('Распознавание речи недоступно в этом браузере. Используйте текстовый ввод.');
  // Озвучка не должна попадать в микрофон.
  stopSpeaking();
  const recognition = new Constructor();
  recognition.lang = 'ru-RU';
  recognition.interimResults = true;
  recognition.continuous = false;
  recognition.onresult = (event) => {
    const results = Array.from(event.results);
    const text = results.map((result) => result[0].transcript).join(' ').trim();
    onText(text, results.length > 0 && results.every((result) => result.isFinal));
  };
  recognition.onerror = (event) => {
    if (event.error === 'aborted') return; // остановлено самим пользователем
    onError(describeError(event.error));
  };
  recognition.onend = onEnd;
  try {
    recognition.start();
  } catch {
    throw new Error('Запись уже идёт. Дождитесь окончания и повторите.');
  }
  return recognition;
}

export function speechSupported(): boolean {
  return 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window;
}

const VOICE_KEY = 'gorky.voice';
// Скорость речи: 1 — обычная. Если речь кажется торопливой, попробуйте 0.9–0.95.
const SPEECH_RATE = 1;

function readPreferredVoice(): string | null {
  try { return window.localStorage.getItem(VOICE_KEY); } catch { return null; }
}

/** Запоминает выбранный пользователем голос; null возвращает автоматический выбор. */
export function setPreferredVoice(name: string | null) {
  try {
    if (name) window.localStorage.setItem(VOICE_KEY, name);
    else window.localStorage.removeItem(VOICE_KEY);
  } catch { /* хранилище недоступно: останется автоматический выбор */ }
}

// Голос по умолчанию, пока пользователь не выбрал другой. Ищется по части названия,
// например «Microsoft Dmitry Online (Natural)» в Edge. Чтобы сменить, поправьте список.
const DEFAULT_VOICE_HINTS = ['dmitry', 'дмитрий'];

// Нейросетевые голоса звучат заметно естественнее старых «настольных» синтезаторов.
function voiceScore(voice: SpeechSynthesisVoice): number {
  const name = (voice.name || '').toLowerCase();
  let score = 0;
  if (DEFAULT_VOICE_HINTS.some((hint) => name.includes(hint))) score += 200;
  if (name.includes('natural') || name.includes('neural')) score += 100;
  if (name.includes('google')) score += 50;
  if (name.includes('online')) score += 20;
  if (name.includes('desktop')) score -= 20;
  return score;
}

/** Русские голоса, лучшие первыми. */
export function listRussianVoices(): SpeechSynthesisVoice[] {
  if (!speechSupported()) return [];
  return window.speechSynthesis.getVoices()
    .filter((voice) => voice.lang.toLowerCase().startsWith('ru'))
    .sort((a, b) => voiceScore(b) - voiceScore(a));
}

/** Голоса в Chrome могут загружаться асинхронно: пустой список ещё не значит «нет голоса». */
export function findRussianVoice(): SpeechSynthesisVoice | null {
  const voices = listRussianVoices();
  const preferred = readPreferredVoice();
  return voices.find((voice) => voice.name === preferred) ?? voices[0] ?? null;
}

/** Подписка на появление голосов; возвращает функцию отписки. */
export function onVoicesChanged(callback: () => void): () => void {
  if (!speechSupported()) return () => {};
  const synth = window.speechSynthesis;
  synth.addEventListener('voiceschanged', callback);
  return () => synth.removeEventListener('voiceschanged', callback);
}

// Ссылки держим, чтобы сборщик мусора не оборвал озвучку (известная проблема Chrome).
let activeUtterances: SpeechSynthesisUtterance[] = [];

function toSpeakable(text: string): string[] {
  const plain = text.replace(/[*_`#>]/g, ' ').replace(/\s+/g, ' ').trim();
  // Chrome обрывает слишком длинные реплики, поэтому озвучиваем по предложениям.
  return plain.split(/(?<=[.!?…])\s+/).filter(Boolean);
}

/** Возвращает false, если озвучка недоступна; тогда ответ остаётся только текстом. */
export function speak(text: string, onEnd?: () => void, onError?: (message: string) => void): boolean {
  if (!speechSupported()) return false;
  const synth = window.speechSynthesis;
  const voices = synth.getVoices();
  if (voices.length > 0 && !findRussianVoice()) {
    onError?.('В системе нет русского голоса: ответ показан только текстом.');
    return false;
  }
  synth.cancel();
  const parts = toSpeakable(text);
  if (parts.length === 0) return false;
  const voice = findRussianVoice();
  activeUtterances = parts.map((part, index) => {
    const utterance = new SpeechSynthesisUtterance(part);
    utterance.lang = 'ru-RU';
    utterance.rate = SPEECH_RATE;
    if (voice) utterance.voice = voice;
    if (index === parts.length - 1) utterance.onend = () => onEnd?.();
    utterance.onerror = (event) => {
      if (event.error === 'canceled' || event.error === 'interrupted') return;
      onError?.('Не удалось озвучить ответ. Он остаётся в тексте.');
    };
    return utterance;
  });
  activeUtterances.forEach((utterance) => synth.speak(utterance));
  return true;
}

export function stopSpeaking() {
  if (!speechSupported()) return;
  window.speechSynthesis.cancel();
  activeUtterances = [];
}
