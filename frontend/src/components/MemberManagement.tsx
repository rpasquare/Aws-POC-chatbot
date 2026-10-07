import { useCallback, useEffect, useState } from "react";
import { type PendingInvite, cancelInvite, listPendingInvites, sendInvite } from "../lib/api";
import "./MemberManagement.css";

interface MemberManagementProps {
  /** Requirement 12.1: the current project's name, displayed directly in
   * this view so it reads as scoped to one project rather than a global
   * or account-level settings area. Passed in rather than fetched here --
   * the caller (`App.tsx`) already has it in hand.
   */
  projectName: string;
}

/** Minimal member management area: send invites, see pending ones, cancel
 * them. The project-invites feature's requirements document calls for this
 * to live in "the project's existing member-management area" -- no such
 * area exists yet (project membership UI is a later, separate sprint), so
 * this is a small self-contained one, built only to host what this feature
 * needs rather than block on unrelated future work.
 */
export function MemberManagement({ projectName }: MemberManagementProps) {
  const [invites, setInvites] = useState<PendingInvite[]>([]);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<"editor" | "viewer">("editor");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const list = await listPendingInvites();
    setInvites(list);
  }, []);

  useEffect(() => {
    refresh().catch((err) => setError(String(err)));
  }, [refresh]);

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      setError(null);
      setSending(true);
      try {
        await sendInvite(email, role);
        setEmail("");
        await refresh();
      } catch (err) {
        // Requirement 6.2: show the specific rejection reason (already a
        // member, already pending, or the email failed to send), not a
        // generic error. `request()` in lib/api.ts already extracts the
        // backend's specific `detail` message onto `err.message`.
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setSending(false);
      }
    },
    [email, role, refresh],
  );

  const handleCancel = useCallback(
    async (inviteId: string) => {
      await cancelInvite(inviteId);
      await refresh();
    },
    [refresh],
  );

  return (
    <div className="members">
      {/* Requirement 12.1, 12.2: the project name is visible directly in
          this view, not just implied by sidebar navigation, so an admin
          can't mistake this for a global or account-level settings screen. */}
      <p className="project-scope">
        Project: <strong>{projectName}</strong>
      </p>
      <p className="eyebrow">Invite someone</p>
      <form className="invite-form" onSubmit={handleSubmit}>
        <input
          type="email"
          placeholder="email@example.com"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          required
        />
        <select value={role} onChange={(e) => setRole(e.target.value as "editor" | "viewer")}>
          <option value="editor">Editor</option>
          <option value="viewer">Viewer</option>
        </select>
        <button type="submit" className="primary" disabled={sending}>
          {sending ? "Sending…" : "Send invite"}
        </button>
      </form>
      {error && <p className="error-hint">{error}</p>}

      <p className="eyebrow" style={{ marginTop: 24 }}>
        Pending invites · {invites.length}
      </p>
      <div className="invite-list">
        {invites.map((invite) => (
          <div key={invite.id} className="invite-row">
            <div>
              <div className="invite-email">{invite.email}</div>
              <div className="invite-meta">
                {invite.role} · invited by {invite.invited_by_email}
              </div>
            </div>
            <button className="cancel" onClick={() => handleCancel(invite.id)}>
              Cancel
            </button>
          </div>
        ))}
        {invites.length === 0 && <p className="empty-hint">No pending invites.</p>}
      </div>
    </div>
  );
}
