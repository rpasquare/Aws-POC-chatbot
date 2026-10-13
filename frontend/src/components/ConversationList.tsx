import { useCallback, useEffect, useState } from "react";
import { type ConversationSummary, createConversation, listConversations } from "../lib/api";
// Explicit import of the shared .primary/.error-hint/.empty-hint
// primitives this component relies on (Requirement 13.1).
import "../App.css";
import "./ConversationList.css";

interface ConversationListProps {
  selectedId: string | null;
  onSelect: (conversationId: string) => void;
  /** Requirement 3.5: hidden entirely for viewers, not merely disabled --
   * the server rejects the attempt either way, and the role chip already
   * explains why. Defaults to true, matching `DocumentList`'s `canUpload`
   * precedent, so this is a no-op while a role hasn't resolved yet.
   */
  canCreate?: boolean;
}

function formatRelative(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString();
}

export function ConversationList({ selectedId, onSelect, canCreate = true }: ConversationListProps) {
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const list = await listConversations();
    setConversations(list);
    return list;
  }, []);

  useEffect(() => {
    refresh().catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [refresh]);

  const handleCreate = useCallback(async () => {
    setError(null);
    setCreating(true);
    try {
      const conversation = await createConversation();
      await refresh();
      onSelect(conversation.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  }, [refresh, onSelect]);

  return (
    <div>
      <p className="eyebrow">Conversations · {conversations.length}</p>
      <div id="conversationList">
        {conversations.map((c) => (
          <button
            key={c.id}
            className={`conv ${c.id === selectedId ? "selected" : ""}`}
            onClick={() => onSelect(c.id)}
          >
            <span className="nm">{c.title}</span>
            <span className="st">{formatRelative(c.last_active)}</span>
          </button>
        ))}
        {conversations.length === 0 && (
          <p className="empty-hint">No conversations yet. Ask a question to start one.</p>
        )}
      </div>

      {canCreate && (
        <button className="primary" disabled={creating} onClick={handleCreate}>
          {creating ? "Starting…" : "+ New conversation"}
        </button>
      )}
      {error && <p className="error-hint">{error}</p>}
    </div>
  );
}
