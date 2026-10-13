import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { AcceptInvitePage } from "./AcceptInvitePage";
import { SignInPage } from "./SignInPage";
import "./theme.css";

// No router dependency exists yet; the accept-invite and sign-in pages
// are reachable without being signed in and live outside the
// authenticated app shell, so they are selected directly by path rather
// than adding a router for two routes.
const page =
  window.location.pathname === "/accept-invite" ? (
    <AcceptInvitePage />
  ) : window.location.pathname === "/sign-in" ? (
    <SignInPage />
  ) : (
    <App />
  );

createRoot(document.getElementById("root")!).render(<StrictMode>{page}</StrictMode>);
