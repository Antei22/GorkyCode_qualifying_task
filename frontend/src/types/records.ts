import type { ChatResponse } from './chat'

export type UserDuration =
  | { type: 'exact' | 'approximate'; minutes: number; evidence?: string }
  | { type: 'range'; min_minutes: number; max_minutes: number; evidence?: string }
  | { type: 'qualitative'; value: 'long' | 'short'; evidence?: string };

export interface RecordItem {
  id: string;
  kind: 'task' | 'meeting' | 'reminder';
  kind_source: string;
  text: string;
  scheduled_date: string | null;
  scheduled_end_date: string | null;
  scheduled_time: string | null;
  duration_minutes: number | null;
  duration_source: 'user' | 'estimated' | 'unknown';
  duration_description: string | null;
  user_duration: UserDuration | null;
  duration_status: 'missing' | 'provided' | 'declined_to_specify' | 'defaulted';
  blocking_duration_minutes: number | null;
  blocking_duration_source:
    | 'user_exact'
    | 'user_approximate'
    | 'user_range_max'
    | 'qualitative_default'
    | 'event_type_default'
    | 'legacy_unknown';
  reserved_until: string | null;
  timezone: string;
  status: string;
  source: string;
}

export interface Session {
  id: string;
  now: string;
  timezone: string;
  fixed_clock: boolean;
  llm_configured: boolean;
  pending: object | null;
}

export interface AssistantResponse extends ChatResponse {
  records: RecordItem[];
  pending: object | null;
}
