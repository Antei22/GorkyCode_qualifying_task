import { useState } from "react";
import type { ChatSource } from "../../types/chat";

interface SourceDisclosureProps {
  source: ChatSource;
}

export function SourceDisclosure({ source }: SourceDisclosureProps) {
  const [isOpen, setIsOpen] = useState(false);

  return (
    <div className="source-disclosure">
      <button
        type="button"
        className="source-disclosure__toggle"
        onClick={() => setIsOpen((prev) => !prev)}
        aria-expanded={isOpen}
      >
        <span className="source-disclosure__icon">{isOpen ? "▾" : "▸"}</span>
        Источник: {source.title}
      </button>
      {isOpen && (
        <div className="source-disclosure__evidence">
          <span className="source-disclosure__evidence-label">{source.type}</span>
          <p>{source.evidence}</p>
        </div>
      )}
    </div>
  );
}
