import { useState } from "react";
import { acceptInvite } from "./lib/api";
// Requirement 13.5: see SignInPage.tsx -- explicit import of the shared
// primitives this page's "needs-password" form and error states rely on.
import "./App.css";
import "./AcceptInvitePage.css";

/** The accept-invite page: reachable without being signed in, since the
 * token in the URL is itself the credential. No router dependency exists
 * in this project yet, so this is selected directly in main.tsx based on
 * the URL path rather than adding one for a single route.
 */
export function AcceptInvitePage() {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("token") ?? "";

  const [password, setPassword] = useState("");
  const [state, setState] = useState<"idle" | "submitting" | "invalid" | "needs-password" | "done">(
    "idle",
  );
  // Separate from `state`, which flips to "submitting" transiently: once
  // the backend has told us this is a first-time recipient (422), the
  // password form must stay showing through subsequent submit attempts
  // rather than being replaced by the generic "Checking your invite…"
  // branch while `state === "submitting"`.
  const [needsPassword, setNeedsPassword] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!token) {
    return <InvalidLink message="This invite link is missing its token." />;
  }

  const submit = async (withPassword?: string) => {
    setState("submitting");
    setError(null);
    try {
      const result = await acceptInvite(token, withPassword);
      setState("done");
      // Requirement 2.3/3.1: land the recipient directly in the project.
      // No project-level page exists yet (future sprint), so this returns
      // to the app shell, which is the closest existing equivalent.
      window.setTimeout(() => {
        window.location.href = `/?project=${result.project_id}`;
      }, 800);
    } catch (err) {
      const status = (err as { status?: number }).status;
      const message = err instanceof Error ? err.message : String(err);
      if (status === 410) {
        setState("invalid");
      } else if (status === 422) {
        // First-time recipient: the backend requires a password.
        setNeedsPassword(true);
        setState("needs-password");
      } else {
        setState(needsPassword ? "needs-password" : "idle");
        setError(message);
      }
    }
  };

  // Try once without a password immediately: a returning recipient
  // succeeds right away (Requirement 3.1); a first-time recipient gets
  // redirected into the password step by the 422 branch above.
  if (state === "idle" && !error) {
    submit();
  }

  if (state === "invalid") {
    return <InvalidLink message="This invite link is no longer valid. Ask the project admin to send a new one." />;
  }

  if (state === "done") {
    return (
      <div className="accept-invite">
        <p className="accept-title">You're in. Taking you to the project…</p>
      </div>
    );
  }

  if (needsPassword) {
    // Requirement 13.3: the button must show a loading label and become
    // disabled while `submit(password)` is in flight.
    const submitting = state === "submitting";
    return (
      <div className="accept-invite">
        <p className="accept-title">Set a password to finish joining</p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            submit(password);
          }}
        >
          <input
            type="password"
            placeholder="Choose a password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={8}
            disabled={submitting}
          />
          <button type="submit" className="primary" disabled={submitting}>
            {submitting ? "Joining…" : "Join project"}
          </button>
        </form>
        {error && <p className="error-hint">{error}</p>}
      </div>
    );
  }

  return (
    <div className="accept-invite">
      <p className="accept-title">Checking your invite…</p>
      {error && <p className="error-hint">{error}</p>}
    </div>
  );
}

function InvalidLink({ message }: { message: string }) {
  return (
    <div className="accept-invite">
      <p className="accept-title">Link not valid</p>
      <p className="error-hint">{message}</p>
    </div>
  );
}
