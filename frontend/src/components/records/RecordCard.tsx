import type { RecordItem } from '../../types/records'

const labels: Record<RecordItem['kind'], string> = {
  task: 'Задача',
  meeting: 'Встреча',
  reminder: 'Напоминание',
};

export function isRecordCompleted(record: RecordItem): boolean {
  return record.status === 'done';
}

interface RecordCardProps {
  record: RecordItem;
  busy: boolean;
  onToggle: (record: RecordItem) => void;
}

export function RecordCard({ record, busy, onToggle }: RecordCardProps) {
  const completed = isRecordCompleted(record);
  return <article className={`record record--${record.kind}${completed ? ' record--done' : ''}`}>
    <button type="button" role="checkbox" className={`record-check${completed ? ' is-done' : ''}`}
      aria-label={completed ? `Вернуть в невыполненные: ${record.text}` : `Отметить выполненным: ${record.text}`}
      aria-checked={completed} disabled={busy || record.status === 'cancelled'}
      onClick={() => onToggle(record)}>{completed ? '✓' : ''}</button>
    <div className="record-main"><span className="badge">{labels[record.kind]}</span><h3>{record.text}</h3>
      <small>{record.timezone} · {record.id.slice(0, 8)}</small></div>
    <time>{record.scheduled_date ? new Date(`${record.scheduled_date}T00:00:00`).toLocaleDateString('ru-RU', { day: '2-digit', month: 'short', year: 'numeric' }) : 'Без даты'}{record.scheduled_time ? `, ${record.scheduled_time.slice(0, 5)}` : ''}</time>
  </article>;
}
