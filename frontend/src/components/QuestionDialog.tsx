import { useState } from "react";
import { Icon } from "./Icon";

// The agent asked something only the user can answer (the AskUser tool). It is
// waiting for this answer, so the dialog cannot be dismissed without one — the
// user can always stop the task instead.

export interface Question {
  id: string;
  question: string;
  options: string[];
}

export function QuestionDialog({ request, onAnswer }: { request: Question; onAnswer: (text: string) => void }) {
  const [text, setText] = useState("");
  return (
    <div className="modal-backdrop">
      <div className="modal question" role="dialog" aria-labelledby="question-title">
        <div className="modal-head">
          <Icon name="chat" />
          <strong id="question-title">The agent is asking</strong>
        </div>
        <p className="question-text">{request.question}</p>
        {request.options.length > 0 && (
          <div className="question-options">
            {request.options.map((option) => (
              <button key={option} onClick={() => onAnswer(option)}>{option}</button>
            ))}
          </div>
        )}
        <textarea
          value={text}
          rows={3}
          autoFocus
          placeholder={request.options.length ? "…or answer in your own words" : "Your answer"}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && text.trim()) {
              e.preventDefault();
              onAnswer(text.trim());
            }
          }}
        />
        <div className="modal-actions">
          <span className="dim small">The task is paused until you answer.</span>
          <div className="spacer" />
          <button className="primary" onClick={() => onAnswer(text.trim())} disabled={!text.trim()}>
            <Icon name="send" size={14} /> Answer
          </button>
        </div>
      </div>
    </div>
  );
}
