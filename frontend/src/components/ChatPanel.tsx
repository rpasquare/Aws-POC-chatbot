import { useCallback, useEffect, useRef, useState } from "react";
import { type MessageSummary, type ModelLog, getConversationMessages, getModelLog, streamAnswer } from "../lib/api";
// Explicit import of the shared .primary/.error-hint/.empty-hint
// primitives this component relies on (Requirement 13.1).
import "../App.css";
import "./ChatPanel.css";

interface ChatPanelProps {
  conversationId: string | null;
  /** Requirement 9.2: viewers can read every conversation but cannot ask
   * a question in it -- the composer is hidden entirely for them, same
   * precedent as `ConversationList`'s `canCreate` and `DocumentList`'s
   * `canUpload`.
   */
  canAsk?: boolean;
}

/** A message as rendered locally. Mirrors `MessageSummary`, but allows a
 * `pending` assistant message to exist before the server has assigned it
 * a real id -- the local id is a client-side placeholder swapped out once
 * `getConversationMessages` is re-fetched after the stream completes.
 */
interface LocalMessage extends MessageSummary {
  pending?: boolean;
}

function CitationChips({ citations }: { citations: MessageSummary["citations"] }) {
  if (citations.length === 0) return null;
  return (
    <span className="citations">
      {citations.map((c) => {
        const label = c.available && c.filename ? `${c.filename}, page ${c.page_from}` : "source unavailable";
        return (
          <span key={c.marker} className={`chip ${c.available ? "" : "unavailable"}`} title={label}>
            [{c.marker}]
            {c.available && c.page_from != null && <span className="chip-page"> p.{c.page_from}</span>}
          </span>
        );
      })}
    </span>
  );
}

/** Requirement 16.18/16.19: shows the exact masked prompt sent to the
 * answer model (large) and the exact raw response received from it
 * (small, pre-substitution), as distinct, clearly labelled sections.
 */
function ModelLogPopup({ log, onClose }: { log: ModelLog; onClose: () => void }) {
  return (
    <div className="model-log-overlay" onClick={onClose}>
      <div className="model-log-popup" onClick={(e) => e.stopPropagation()}>
        <div className="model-log-header">
          <h3>Model audit log</h3>
          <button type="button" className="model-log-close" onClick={onClose} aria-label="Close">
            ×
          </button>
        </div>
        <section className="model-log-section">
          <h4>Request sent to the model</h4>
          <pre className="model-log-text model-log-request">{log.request_text}</pre>
        </section>
        <section className="model-log-section">
          <h4>Raw response received</h4>
          <pre className="model-log-text model-log-response">{log.response_text}</pre>
        </section>
      </div>
    </div>
  );
}

function EyeIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2">
      <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7Z" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  );
}

function MessageBubble({ message }: { message: LocalMessage }) {
  const [log, setLog] = useState<ModelLog | null>(null);
  const [loadingLog, setLoadingLog] = useState(false);
  const [logError, setLogError] = useState<string | null>(null);

  const handleViewLog = useCallback(async () => {
    setLogError(null);
    setLoadingLog(true);
    try {
      const result = await getModelLog(message.id);
      setLog(result);
    } catch (err) {
      setLogError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoadingLog(false);
    }
  }, [message.id]);

  if (message.role === "user") {
    return (
      <div className="msg user">
        <div className="bubble">{message.text}</div>
      </div>
    );
  }

  if (!message.grounded && !message.pending) {
    // Requirement 7.3/7.4: the refusal is a distinct kind of message, not
    // a normal grounded answer -- rendered in a muted/italic style so it
    // reads differently at a glance.
    return (
      <div className="msg assistant refusal">
        <div className="bubble">{message.text}</div>
      </div>
    );
  }

  // Nothing has streamed in yet: show a "thinking" indicator rather than
  // an empty bubble, since retrieval plus the model's first token can
  // take a few real seconds and a blank bubble reads as broken, not busy.
  if (message.pending && message.text === "") {
    return (
      <div className="msg assistant">
        <div className="bubble thinking">
          <span className="dot" />
          <span className="dot" />
          <span className="dot" />
        </div>
      </div>
    );
  }

  return (
    <div className="msg assistant">
      <div className="bubble">
        {message.text}
        {message.pending && <span className="cursor" />}
        <CitationChips citations={message.citations} />
        {message.has_model_log && !message.pending && (
          <button
            type="button"
            className="model-log-trigger"
            onClick={handleViewLog}
            disabled={loadingLog}
            title="View what was sent to and received from the model"
            aria-label="View model audit log"
          >
            <EyeIcon />
          </button>
        )}
      </div>
      {logError && <p className="error-hint">{logError}</p>}
      {log && <ModelLogPopup log={log} onClose={() => setLog(null)} />}
    </div>
  );
}

export function ChatPanel({ conversationId, canAsk = true }: ChatPanelProps) {
  const [messages, setMessages] = useState<LocalMessage[]>([]);
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);

  const refresh = useCallback(async (id: string) => {
    const list = await getConversationMessages(id);
    setMessages(list);
  }, []);

  useEffect(() => {
    setMessages([]);
    setError(null);
    if (conversationId) {
      refresh(conversationId).catch((err) => setError(err instanceof Error ? err.message : String(err)));
    }
  }, [conversationId, refresh]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      if (!conversationId || !question.trim()) return;
      setError(null);
      const asked = question;
      setQuestion("");
      setAsking(true);

      // Optimistic local messages: the user's question appears right
      // away, and the assistant's answer fills in progressively as
      // fragments arrive -- Requirement 7.5's whole point is that this
      // must visibly stream rather than wait for the full response.
      const pendingId = `pending-${Date.now()}`;
      setMessages((prev) => [
        ...prev,
        {
          id: `${pendingId}-question`,
          session_id: conversationId,
          role: "user",
          text: asked,
          author_id: null,
          grounded: true,
          created_at: new Date().toISOString(),
          citations: [],
          has_model_log: false,
        },
        {
          id: pendingId,
          session_id: conversationId,
          role: "assistant",
          text: "",
          author_id: null,
          grounded: true,
          created_at: new Date().toISOString(),
          citations: [],
          has_model_log: false,
          pending: true,
        },
      ]);

      try {
        const result = await streamAnswer(conversationId, asked, (fragment) => {
          setMessages((prev) => prev.map((m) => (m.id === pendingId ? { ...m, text: m.text + fragment } : m)));
        });
        setMessages((prev) =>
          prev.map((m) => (m.id === pendingId ? { ...m, grounded: result.grounded, pending: false } : m)),
        );
        // Reconcile with the server's recorded messages (real ids,
        // citations, exact text) now that the stream has completed.
        await refresh(conversationId);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
        setMessages((prev) => prev.filter((m) => m.id !== pendingId));
      } finally {
        setAsking(false);
      }
    },
    [conversationId, question, refresh],
  );

  if (!conversationId) {
    return (
      <div className="chat-empty">
        <p className="empty-hint">Select or start a conversation to see messages.</p>
      </div>
    );
  }

  return (
    <div className="chat-panel">
      <div className="messages">
        {messages.map((m) => (
          <MessageBubble key={m.id} message={m} />
        ))}
        {messages.length === 0 && <p className="empty-hint">No messages yet. Ask a question below.</p>}
        <div ref={bottomRef} />
      </div>

      {canAsk && (
        <form className="composer" onSubmit={handleSubmit}>
          <input
            type="text"
            placeholder="Ask a question about these documents…"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            disabled={asking}
          />
          <button type="submit" className="primary" disabled={asking || !question.trim()}>
            {asking ? "Asking…" : "Ask"}
          </button>
        </form>
      )}
      {error && <p className="error-hint">{error}</p>}
    </div>
  );
}
