import { useEffect, useState } from "react";
import { ChatPanel } from "./components/ChatPanel";
import { ConversationList } from "./components/ConversationList";
import { DocumentList } from "./components/DocumentList";
import { ExtractedTextPanel } from "./components/ExtractedTextPanel";
import { MemberManagement } from "./components/MemberManagement";
import { getMyRole, signOut } from "./lib/api";
import "./App.css";

// Requirement 12.1: the project name shown here is the single source of
// truth for "what project is this" in the member management view too --
// passed down as a prop rather than refetched, since it is already in
// hand and there is only ever one project in the current dev seed data.
const PROJECT_NAME = "Development Project";

export default function App() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedConversationId, setSelectedConversationId] = useState<string | null>(null);
  const [view, setView] = useState<"documents" | "chat" | "members">("documents");
  const [myRole, setMyRole] = useState<string | null>(null);
  const [myEmail, setMyEmail] = useState<string | null>(null);

  // Requirement 7.5: a visible, truthful answer to "what can I actually do
  // here" -- resolved the same way the server resolves it (session first,
  // placeholder fallback), so the chip and the server never disagree. The
  // email shown in the header is likewise whoever actually resolved from
  // the request (real session if present, else the X-User-Id placeholder's
  // own account), not a hardcoded display string -- otherwise the header
  // would keep showing "dev@example.com" even after signing in as someone
  // else via the real sign-in page.
  //
  // Requirement 1.1: if this fails outright (not just "no role on this
  // project", but the request itself erroring -- e.g. no X-User-Id
  // fallback available and no session either) there is nowhere useful to
  // land, so send the visitor to sign in rather than show a broken shell.
  // The X-User-Id placeholder still makes this resolve successfully for
  // local dev, per the project's current trimmed-auth state -- the
  // "sign in" link below is the reliable way to reach the real page.
  useEffect(() => {
    getMyRole()
      .then((r) => {
        setMyRole(r.role);
        setMyEmail(r.email);
      })
      .catch(() => {
        window.location.href = `/sign-in?redirect=${encodeURIComponent(window.location.pathname)}`;
      });
  }, []);

  // Requirement 3.5, 12.3: the member management view is admin-only, and
  // that must hold even if `view` was already "members" when the role
  // resolved to something else (e.g. a stale click before `myRole` loaded,
  // or a role change on refresh) -- not just a hidden nav button that
  // leaves the view itself still reachable.
  useEffect(() => {
    if (view === "members" && myRole !== null && myRole !== "admin") {
      setView("documents");
    }
  }, [view, myRole]);

  return (
    <div>
      <header className="appbar">
        <div className="mark" />
        <div className="brand">Secure Workspace</div>
        <div className="who">
          <div className="avatar">{myEmail ? myEmail.slice(0, 2).toUpperCase() : "?"}</div>
          {myEmail ?? "not signed in"}
          {myEmail ? (
            <button
              className="sign-in-link"
              onClick={async () => {
                await signOut();
                window.location.href = "/sign-in";
              }}
            >
              Sign out
            </button>
          ) : (
            <button className="sign-in-link" onClick={() => (window.location.href = "/sign-in")}>
              Sign in
            </button>
          )}
        </div>
      </header>

      <div className="shell">
        <aside className="rail">
          <p className="eyebrow">Projects</p>
          <button className="proj" aria-current="true">
            <div className="nm">
              Development Project
              {myRole && <span className="role-chip">{myRole}</span>}
            </div>
          </button>
          <button className="primary" style={{ marginTop: 10 }}>
            + Add project
          </button>

          <p className="eyebrow" style={{ marginTop: 24 }}>
            View
          </p>
          <button className="proj" aria-current={view === "documents"} onClick={() => setView("documents")}>
            <div className="nm">Documents</div>
          </button>
          {/* Requirement 9.2/9.3: visible to every role -- admin, editor
              and viewer can all read conversations, only creating one or
              asking a question is restricted (gated inside the view). */}
          <button className="proj" aria-current={view === "chat"} onClick={() => setView("chat")}>
            <div className="nm">Ask questions</div>
          </button>
          {/* Requirement 3.5, 12.3: this nav entry itself must not be in
              the DOM for editors/viewers, not merely disabled. */}
          {myRole === "admin" && (
            <button className="proj" aria-current={view === "members"} onClick={() => setView("members")}>
              <div className="nm">Members &amp; invites</div>
            </button>
          )}
        </aside>

        {view === "members" && myRole === "admin" ? (
          <section className="work" style={{ gridColumn: "span 2" }}>
            <p className="eyebrow">Project invites</p>
            <h2 style={{ fontFamily: "var(--serif)", marginTop: 0 }}>Invite your team</h2>
            <p style={{ color: "var(--muted)", maxWidth: 480, marginBottom: 24 }}>
              Invite someone by email and choose whether they can edit or only view.
            </p>
            <MemberManagement projectName={PROJECT_NAME} />
          </section>
        ) : view === "chat" ? (
          <>
            <section className="mid">
              <ConversationList
                selectedId={selectedConversationId}
                onSelect={setSelectedConversationId}
                canCreate={myRole === "admin" || myRole === "editor"}
              />
            </section>

            <section className="work">
              <p className="eyebrow">Sprint 2 demo</p>
              <h2 style={{ fontFamily: "var(--serif)", marginTop: 0 }}>Ask questions, get cited answers</h2>
              <p style={{ color: "var(--muted)", maxWidth: 480, marginBottom: 24 }}>
                Pick a conversation on the left, or start a new one, and ask about the uploaded
                documents.
              </p>
              <ChatPanel
                conversationId={selectedConversationId}
                canAsk={myRole === "admin" || myRole === "editor"}
              />
            </section>
          </>
        ) : (
          <>
            <section className="mid">
              <DocumentList selectedId={selectedId} onSelect={setSelectedId} canUpload={myRole === "admin"} />
            </section>

            <section className="work">
              <p className="eyebrow">Sprint 1 demo</p>
              <h2 style={{ fontFamily: "var(--serif)", marginTop: 0 }}>Upload a PDF and watch it get parsed</h2>
              <p style={{ color: "var(--muted)", maxWidth: 480, marginBottom: 24 }}>
                Add a document on the left, then click it to see the extracted text once parsing
                finishes.
              </p>
              <ExtractedTextPanel documentId={selectedId} />
            </section>
          </>
        )}
      </div>
    </div>
  );
}
