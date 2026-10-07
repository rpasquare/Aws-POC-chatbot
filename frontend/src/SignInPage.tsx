import { useState } from "react";
import { signIn } from "./lib/api";
import "./SignInPage.css";

/** The sign-in page: reachable without being signed in. No router
 * dependency exists in this project yet, so this is selected directly in
 * main.tsx based on the URL path, mirroring AcceptInvitePage.
 */
export function SignInPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const params = new URLSearchParams(window.location.search);
  const redirectTo = params.get("redirect") || "/";

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      await signIn(email, password);
      window.location.href = redirectTo;
    } catch (err) {
      // Requirement 1.3/15.5: the server already returns one uniform
      // message for both unknown email and wrong password -- this just
      // displays whatever that message is, without adding its own branch.
      const message = err instanceof Error ? err.message : String(err);
      setError(message);
      setSubmitting(false);
    }
  };

  return (
    <div className="sign-in">
      <div className="sign-in-brand">
        <div className="mark" />
        <span>Secure Workspace</span>
      </div>
      <p className="sign-in-title">Sign in</p>
      <form onSubmit={submit}>
        <input
          type="email"
          placeholder="Email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="email"
          required
        />
        <input
          type="password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          required
        />
        <button type="submit" className="primary" disabled={submitting}>
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
      {error && <p className="error-hint">{error}</p>}
    </div>
  );
}
