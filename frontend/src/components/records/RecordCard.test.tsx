import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { RecordItem } from '../../types/records'
import { RecordCard } from './RecordCard'

function record(kind: RecordItem['kind'], status: RecordItem['status']): RecordItem {
  return {
    id: '12345678-0000-0000-0000-000000000000', kind, kind_source: 'user_input',
    text: 'Тестовая запись', scheduled_date: '2026-11-01', scheduled_end_date: null,
    scheduled_time: '12:00:00', duration_minutes: null, duration_source: 'unknown',
    duration_description: null, user_duration: null, duration_status: 'defaulted',
    blocking_duration_minutes: null, blocking_duration_source: 'legacy_unknown',
    reserved_until: null, timezone: 'Europe/Moscow', status, version: 1, source: 'chat',
  };
}

describe('RecordCard completion checkbox', () => {
  it.each([
    ['reminder', 'open', 'false'],
    ['reminder', 'done', 'true'],
    ['meeting', 'open', 'false'],
    ['task', 'done', 'true'],
  ] as const)('%s with %s status has aria-checked=%s', (kind, status, expected) => {
    const html = renderToStaticMarkup(<RecordCard record={record(kind, status)} busy={false} onToggle={() => {}} />);
    expect(html).toContain(`aria-checked="${expected}"`);
    expect(html.includes('is-done')).toBe(status === 'done');
  });

  it('does not render an actions menu', () => {
    const html = renderToStaticMarkup(<RecordCard record={record('meeting', 'open')} busy={false} onToggle={() => {}} />);
    expect(html).not.toContain('•••');
    expect(html).not.toContain('record-menu');
  });
});
