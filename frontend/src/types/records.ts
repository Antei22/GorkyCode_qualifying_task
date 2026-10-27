import type { ChatResponse } from './chat'

export interface RecordItem {
  id: string;
  kind: 'task' | 'meeting' | 'reminder' | null;
  text: string;
  scheduled_date: string | null;
  scheduled_time: string | null;
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
