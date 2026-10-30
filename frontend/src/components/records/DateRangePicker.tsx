import { useMemo, useState } from 'react';

export interface DateRangeValue { from: string | null; to: string | null }
interface Props { value: DateRangeValue; onChange(value: DateRangeValue): void; disabled?: boolean }
const WEEKDAYS = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const isoDate = (year: number, month: number, day: number) => `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
const format = (value: string) => new Date(`${value}T00:00:00`).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });

export function DateRangePicker({ value, onChange, disabled }: Props) {
  const today = new Date();
  const [open, setOpen] = useState(false);
  const [cursor, setCursor] = useState(() => new Date(today.getFullYear(), today.getMonth(), 1));
  const [draftFrom, setDraftFrom] = useState<string | null>(null);
  const cells = useMemo(() => {
    const year = cursor.getFullYear(); const month = cursor.getMonth();
    const leading = (new Date(year, month, 1).getDay() + 6) % 7;
    return [...Array.from({ length: leading }, () => null), ...Array.from({ length: new Date(year, month + 1, 0).getDate() }, (_, i) => isoDate(year, month, i + 1))];
  }, [cursor]);
  const label = !value.from || !value.to ? 'Все даты' : value.from === value.to ? format(value.from) : `${format(value.from)} — ${format(value.to)}`;
  const selectedFrom = draftFrom ?? value.from; const selectedTo = draftFrom ? draftFrom : value.to;
  function pick(day: string) {
    if (!draftFrom) { setDraftFrom(day); return; }
    onChange({ from: day < draftFrom ? day : draftFrom, to: day < draftFrom ? draftFrom : day });
    setDraftFrom(null); setOpen(false);
  }
  function clear() { setDraftFrom(null); onChange({ from: null, to: null }); setOpen(false); }
  return <div className="date-range">
    <button type="button" className="date-range__trigger" disabled={disabled} onClick={() => setOpen((current) => !current)} aria-expanded={open}>
      <span aria-hidden="true">▣</span><span>{label}</span>
    </button>
    {open && <div className="date-range__popover" role="dialog" aria-label="Выбор периода">
      <div className="date-range__header"><button type="button" aria-label="Предыдущий месяц" onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() - 1, 1))}>‹</button><strong>{cursor.toLocaleDateString('ru-RU', { month: 'long', year: 'numeric' })}</strong><button type="button" aria-label="Следующий месяц" onClick={() => setCursor(new Date(cursor.getFullYear(), cursor.getMonth() + 1, 1))}>›</button></div>
      <div className="date-range__weekdays">{WEEKDAYS.map((day) => <span key={day}>{day}</span>)}</div>
      <div className="date-range__grid">{cells.map((day, index) => day ? <button type="button" key={day} className={['date-range__day', selectedFrom && selectedTo && day >= selectedFrom && day <= selectedTo ? 'is-in-range' : '', day === selectedFrom || day === selectedTo ? 'is-edge' : ''].filter(Boolean).join(' ')} onClick={() => pick(day)}>{Number(day.slice(-2))}</button> : <span key={`empty-${index}`} />)}</div>
      <div className="date-range__footer"><span>{draftFrom ? 'Выберите конечную дату' : 'Выберите начало периода'}</span><button type="button" onClick={clear}>Все даты</button></div>
    </div>}
  </div>;
}
