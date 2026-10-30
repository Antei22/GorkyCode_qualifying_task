import { afterEach, describe, expect, it, vi } from 'vitest'
import { findRussianVoice, recognitionSupported, setPreferredVoice, speak, startRecognition } from './browser'

type FakeEvent = { results: Array<{ isFinal: boolean; 0: { transcript: string } }> };

class FakeRecognition {
  static failStart = false;
  static instances: FakeRecognition[] = [];
  lang = '';
  interimResults = false;
  continuous = false;
  onresult: ((event: FakeEvent) => void) | null = null;
  onerror: ((event: { error: string }) => void) | null = null;
  onend: (() => void) | null = null;
  constructor() { FakeRecognition.instances.push(this); }
  start() { if (FakeRecognition.failStart) throw new Error('InvalidStateError'); }
  stop() {}
  abort() {}
}

function last(): FakeRecognition | undefined {
  return FakeRecognition.instances[FakeRecognition.instances.length - 1];
}

function installRecognition() {
  (window as unknown as Record<string, unknown>).SpeechRecognition = FakeRecognition;
}

function installSynthesis(voices: Array<{ lang: string; name?: string }>) {
  const speakMock = vi.fn();
  const cancel = vi.fn();
  vi.stubGlobal('speechSynthesis', { getVoices: () => voices, speak: speakMock, cancel });
  vi.stubGlobal('SpeechSynthesisUtterance', class {
    text: string;
    lang = '';
    voice: unknown = null;
    onend: (() => void) | null = null;
    onerror: ((event: { error: string }) => void) | null = null;
    constructor(text: string) { this.text = text; }
  });
  return { speakMock, cancel };
}

afterEach(() => {
  vi.unstubAllGlobals();
  window.localStorage.clear();
  delete (window as unknown as Record<string, unknown>).SpeechRecognition;
  FakeRecognition.failStart = false;
  FakeRecognition.instances = [];
});

describe('recognition', () => {
  it('сообщает, что распознавания нет, и не падает', () => {
    expect(recognitionSupported()).toBe(false);
    expect(() => startRecognition(() => {}, () => {}, () => {})).toThrow(/недоступно/);
  });

  it('отличает промежуточный результат от окончательного', () => {
    installRecognition();
    const onText = vi.fn();
    startRecognition(onText, () => {}, () => {});
    last()?.onresult?.({ results: [{ isFinal: false, 0: { transcript: 'напомни завтра' } }] });
    last()?.onresult?.({ results: [{ isFinal: true, 0: { transcript: 'напомни завтра в десять' } }] });
    expect(onText).toHaveBeenNthCalledWith(1, 'напомни завтра', false);
    expect(onText).toHaveBeenNthCalledWith(2, 'напомни завтра в десять', true);
  });

  it('переводит коды ошибок в понятный текст и игнорирует aborted', () => {
    installRecognition();
    const onError = vi.fn();
    startRecognition(() => {}, onError, () => {});
    last()?.onerror?.({ error: 'aborted' });
    expect(onError).not.toHaveBeenCalled();
    last()?.onerror?.({ error: 'not-allowed' });
    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError.mock.calls[0][0]).toMatch(/микрофон/i);
  });

  it('превращает ошибку повторного start() в понятное сообщение', () => {
    installRecognition();
    FakeRecognition.failStart = true;
    expect(() => startRecognition(() => {}, () => {}, () => {})).toThrow(/Запись уже идёт/);
  });
});

describe('speech synthesis', () => {
  it('возвращает false, если синтеза речи нет', () => {
    expect(speak('Привет')).toBe(false);
  });

  it('не озвучивает без русского голоса и сообщает об этом', () => {
    const { speakMock } = installSynthesis([{ lang: 'en-US' }]);
    const onError = vi.fn();
    expect(speak('Привет', undefined, onError)).toBe(false);
    expect(speakMock).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledOnce();
  });

  it('отменяет прошлую озвучку и читает ответ по предложениям', () => {
    const { speakMock, cancel } = installSynthesis([{ lang: 'ru-RU' }]);
    expect(speak('Встреча сохранена. Завтра в 10:00.')).toBe(true);
    expect(cancel).toHaveBeenCalled();
    expect(speakMock).toHaveBeenCalledTimes(2);
  });

  const edgeVoices = [
    { lang: 'ru-RU', name: 'Microsoft Irina Desktop - Russian' },
    { lang: 'ru-RU', name: 'Microsoft Svetlana Online (Natural) - Russian (Russia)' },
    { lang: 'ru-RU', name: 'Microsoft Dmitry Online (Natural) - Russian (Russia)' },
  ];

  it('по умолчанию выбирает голос Дмитрий, если он доступен', () => {
    installSynthesis(edgeVoices);
    expect(findRussianVoice()?.name).toContain('Dmitry');
  });

  it('использует голос, выбранный пользователем', () => {
    installSynthesis(edgeVoices);
    setPreferredVoice('Microsoft Svetlana Online (Natural) - Russian (Russia)');
    expect(findRussianVoice()?.name).toContain('Svetlana');
  });

  it('без Дмитрия выбирает нейросетевой голос вместо старого', () => {
    installSynthesis(edgeVoices.slice(0, 2));
    expect(findRussianVoice()?.name).toContain('Svetlana');
  });
});
