import type { ChatMessage } from "../../types/chat";
import { SourceDisclosure } from "./SourceDisclosure";

interface MessageBubbleProps {
  message: ChatMessage;
}
export function MessageBubble({ message }: MessageBubbleProps) {
  const isUser = message.role === "user";

  return (
    <div className={`message-row ${isUser ? "message-row--user" : "message-row--assistant"}`}>
      <div className={`message-bubble ${message.isError ? "message-bubble--error" : ""}`}>
        <p>{message.content}</p>

        {message.sources && message.sources.length > 0 && (
          <div className="message-bubble__sources">
            {message.sources.map((source, index) => (
              <SourceDisclosure key={`${message.id}-source-${index}`} source={source} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
