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

export function recognitionSupported(): boolean {
  const target = window as VoiceWindow;
  return Boolean(target.SpeechRecognition ?? target.webkitSpeechRecognition);
}

export function startRecognition(onText: (text: string) => void,
  onError: (message: string) => void, onEnd: () => void): Recognition {
  const target = window as VoiceWindow;
  const Constructor = target.SpeechRecognition ?? target.webkitSpeechRecognition;
  if (!Constructor) throw new Error('Распознавание речи недоступно в этом браузере. Используйте текстовый ввод.');
  const recognition = new Constructor();
  recognition.lang = 'ru-RU';
  recognition.interimResults = true;
  recognition.continuous = false;
  recognition.onresult = (event) => {
    onText(Array.from(event.results).map((result) => result[0].transcript).join(' '));
  };
  recognition.onerror = (event) => onError(`Ошибка распознавания: ${event.error}. Можно ввести запрос текстом.`);
  recognition.onend = onEnd;
  recognition.start();
  return recognition;
}

export function speak(text: string) {
  if (!('speechSynthesis' in window)) return;
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = 'ru-RU';
  window.speechSynthesis.speak(utterance);
}

export function stopSpeaking() {
  if ('speechSynthesis' in window) window.speechSynthesis.cancel();
}
