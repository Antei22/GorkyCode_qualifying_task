export interface ChatSource {
  type: string;
  title: string;
  evidence: string;
}

export interface ChatRequest {
  request_id: string;
  message: string;
}

export interface ChatResponse {
  answer: string;
  sources: ChatSource[];
}

export type ChatRole = "user" | "assistant";

export interface ChatMessage {
  id: string;
  role: ChatRole;
  content: string;
  sources?: ChatSource[];
  isError?: boolean;
}
