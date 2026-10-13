/** Thin client for the Deal Workspace API.
 *
 * `X-User-Id` stands in for a resolved session until real authentication
 * lands (Sprint 4). The dev user and project ids match the seed data.
 */

const API_BASE = "http://localhost:8000";
export const DEV_USER_ID = "00000000-0000-0000-0000-000000000001";
export const DEV_PROJECT_ID = "00000000-0000-0000-0000-0000000000a1";

/** Name of the double-submit CSRF cookie the backend sets alongside the
 * session cookie (`app/csrf.py`). Deliberately not HttpOnly server-side,
 * so it can be read here and echoed back as a header. Requirement 15.6.
 */
const CSRF_COOKIE_NAME = "csrf_token";
const CSRF_HEADER_NAME = "X-CSRF-Token";

/** Read a cookie's value by name from `document.cookie`. There is no
 * existing cookie-parsing helper in this file -- the session cookie is
 * HttpOnly and never read from JS, but the CSRF cookie is designed to be.
 */
function readCookie(name: string): string | null {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const trimmed = part.trim();
    if (trimmed.startsWith(prefix)) {
      return decodeURIComponent(trimmed.slice(prefix.length));
    }
  }
  return null;
}

/** CSRF header for a non-GET request, if the CSRF cookie is present (i.e.
 * a real session exists). Omitted entirely when there is no cookie, so
 * placeholder-only (`X-User-Id`) requests are unaffected -- the backend
 * only requires the header when a session cookie is also present.
 */
function csrfHeaders(method: string | undefined): Record<string, string> {
  const normalized = (method ?? "GET").toUpperCase();
  if (normalized === "GET" || normalized === "HEAD") return {};
  const token = readCookie(CSRF_COOKIE_NAME);
  return token ? { [CSRF_HEADER_NAME]: token } : {};
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    // Without this, the browser never attaches the session cookie on a
    // cross-origin request (frontend :5174, API :8000 count as different
    // origins) -- every call through this helper would silently fall back
    // to the X-User-Id placeholder on the server even while signed in.
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-User-Id": DEV_USER_ID,
      ...csrfHeaders(init?.method),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      if (typeof parsed?.detail === "string") detail = parsed.detail;
    } catch {
      // body wasn't JSON (or had no .detail) -- fall back to the raw text
    }
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export interface UploadSlot {
  document_id: string;
  upload_url: string;
}

export interface DocumentSummary {
  id: string;
  filename: string;
  status: string;
  page_count: number | null;
  failure_reason: string | null;
  created_at: string;
  ready_at: string | null;
}

export function createUploadSlot(file: File): Promise<UploadSlot> {
  return request(`/api/projects/${DEV_PROJECT_ID}/documents`, {
    method: "POST",
    body: JSON.stringify({
      filename: file.name,
      content_type: file.type || "application/pdf",
      byte_size: file.size,
    }),
  });
}

export async function uploadToS3(uploadUrl: string, file: File): Promise<void> {
  const res = await fetch(uploadUrl, {
    method: "PUT",
    headers: { "Content-Type": file.type || "application/pdf" },
    body: file,
  });
  if (!res.ok) {
    throw new Error(`upload to storage failed: ${res.status} ${res.statusText}`);
  }
}

export interface CompletionResult {
  document_id: string;
  duplicate: boolean;
}

export function completeUpload(documentId: string): Promise<CompletionResult> {
  return request(`/api/documents/${documentId}/complete`, { method: "POST" });
}

export function listDocuments(): Promise<DocumentSummary[]> {
  return request(`/api/projects/${DEV_PROJECT_ID}/documents`);
}

export function getDocument(documentId: string): Promise<DocumentSummary> {
  return request(`/api/documents/${documentId}`);
}

export interface ExtractedBlock {
  page_no: number;
  text: string;
  bbox: [number, number, number, number];
  kind: string;
  heading_path: string | null;
}

export interface ExtractedText {
  pages: { page_no: number; width: number; height: number }[];
  blocks: ExtractedBlock[];
}

export function getExtractedText(documentId: string): Promise<ExtractedText> {
  return request(`/api/documents/${documentId}/extracted-text`);
}

// --- Sign in / sign out ----------------------------------------------------

export interface SignInResult {
  user_id: string;
}

/** Sign in with email and password. Like `acceptInvite`, this carries the
 * browser's session cookie (`credentials: "include"`) rather than the
 * X-User-Id placeholder, since the caller is not signed in yet -- the
 * response is what sets the real session cookie. No CSRF header is sent
 * (or required server-side): there is no prior session to forge yet, this
 * request is what establishes one. See `app/csrf.py`'s `EXEMPT_PATHS`.
 */
export async function signIn(email: string, password: string): Promise<SignInResult> {
  const res = await fetch(`${API_BASE}/api/auth/session`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      if (typeof parsed?.detail === "string") detail = parsed.detail;
    } catch {
      // body wasn't JSON -- fall back to the raw text
    }
    const error = new Error(detail || `${res.status} ${res.statusText}`) as Error & { status: number };
    error.status = res.status;
    throw error;
  }
  return res.json() as Promise<SignInResult>;
}

/** Sign out: tells the server to delete the session row, then relies on
 * the server's `delete_cookie` response to clear the cookie client-side.
 * Relies on the session cookie, so it needs the CSRF header too.
 */
export async function signOut(): Promise<void> {
  await fetch(`${API_BASE}/api/auth/session`, {
    method: "DELETE",
    credentials: "include",
    headers: { ...csrfHeaders("DELETE") },
  });
}

export interface MyRole {
  role: string | null;
  email: string | null;
}

/** The caller's own role on the current project, resolved from a real
 * session when one is present (set by `credentials: "include"` below),
 * falling back to the X-User-Id placeholder otherwise. Used for the role
 * chip and for hiding role-inappropriate controls.
 */
export function getMyRole(): Promise<MyRole> {
  return request(`/api/projects/${DEV_PROJECT_ID}/me`, { credentials: "include" });
}

// --- Project invites -------------------------------------------------------

export interface PendingInvite {
  id: string;
  email: string;
  role: string;
  invited_by_email: string;
  created_at: string;
}

export interface InviteResponse {
  invite_id: string;
}

export function sendInvite(email: string, role: "editor" | "viewer"): Promise<InviteResponse> {
  return request(`/api/projects/${DEV_PROJECT_ID}/invites`, {
    method: "POST",
    body: JSON.stringify({ email, role }),
  });
}

export function listPendingInvites(): Promise<PendingInvite[]> {
  return request(`/api/projects/${DEV_PROJECT_ID}/invites`);
}

export function cancelInvite(inviteId: string): Promise<{ status: string }> {
  return request(`/api/projects/${DEV_PROJECT_ID}/invites/${inviteId}/cancel`, {
    method: "POST",
  });
}

export interface AcceptInviteResult {
  project_id: string;
  first_time: boolean;
}

/** Unlike `request`, this carries the browser's session cookie
 * (`credentials: "include"`) rather than the X-User-Id placeholder, since
 * the caller is not signed in yet -- the invite token itself is the
 * credential, and the response sets a real session cookie. No CSRF
 * header required, for the same reason as `signIn`: this is what
 * establishes the session, not a request that relies on one.
 */
export async function acceptInvite(token: string, password?: string): Promise<AcceptInviteResult> {
  const res = await fetch(`${API_BASE}/api/invites/accept`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token, password }),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      if (typeof parsed?.detail === "string") detail = parsed.detail;
    } catch {
      // body wasn't JSON -- fall back to the raw text
    }
    const error = new Error(detail || `${res.status} ${res.statusText}`) as Error & { status: number };
    error.status = res.status;
    throw error;
  }
  return res.json() as Promise<AcceptInviteResult>;
}

// --- Conversations and messages --------------------------------------------

export interface ConversationSummary {
  id: string;
  project_id: string;
  title: string;
  created_by: string;
  created_at: string;
  last_active: string;
}

export interface CitationSummary {
  marker: number;
  chunk_id: string | null;
  document_id: string | null;
  available: boolean;
  filename: string | null;
  page_from: number | null;
}

export interface MessageSummary {
  id: string;
  session_id: string;
  role: "user" | "assistant";
  text: string;
  author_id: string | null;
  grounded: boolean;
  created_at: string;
  citations: CitationSummary[];
  /** Requirement 16.18: true when a model audit log exists for this
   * message (always false for refusals, Requirement 16.20) -- lets the
   * client decide whether to render the eye icon without an extra
   * round trip per message.
   */
  has_model_log: boolean;
}

export interface ModelLog {
  message_id: string;
  request_text: string;
  response_text: string;
}

/** Fetch the exact masked prompt sent to, and raw response received
 * from, the answer model for one message (Requirement 16.18). Only
 * called when `has_model_log` is true for that message.
 */
export function getModelLog(messageId: string): Promise<ModelLog> {
  return request(`/api/messages/${messageId}/model-log`);
}

/** Start a new conversation. Admin/editor only server-side -- a viewer's
 * call fails with 403 (surfaced via `request()`'s thrown Error).
 */
export function createConversation(title?: string): Promise<ConversationSummary> {
  return request(`/api/projects/${DEV_PROJECT_ID}/conversations`, {
    method: "POST",
    body: JSON.stringify({ title: title ?? null }),
  });
}

/** List a project's conversations, most recently active first. Readable
 * by any member regardless of role.
 */
export function listConversations(): Promise<ConversationSummary[]> {
  return request(`/api/projects/${DEV_PROJECT_ID}/conversations`);
}

/** List a conversation's messages in order, with citations attached. */
export function getConversationMessages(conversationId: string): Promise<MessageSummary[]> {
  return request(`/api/conversations/${conversationId}/messages`);
}

/** Ask a question in a conversation, streaming the assistant's answer as
 * it is generated (Requirement 7.5). `request()` can't be reused here --
 * it always awaits and parses a full JSON body, which is exactly what a
 * streaming response must not do -- so this talks to `fetch` directly.
 *
 * Two distinct server responses are both handled by this one function:
 *  - A grounded answer: `StreamingResponse` with `media_type=text/plain`,
 *    body arriving as plain text fragments. `onFragment` is called once
 *    per chunk as it is read off the stream.
 *  - A refusal: a single, non-streamed `JSONResponse` shaped like
 *    `MessageSummary` (grounded: false, citations: []). Detected by
 *    content-type rather than assumed, and reported as one fragment
 *    followed immediately by `done`, so callers don't need two code
 *    paths to render it.
 *
 * Returns the full final text once the stream (or refusal body) has been
 * completely read, so callers can reconcile their optimistic local state
 * without a second round-trip if they don't want one.
 */
export async function streamAnswer(
  conversationId: string,
  question: string,
  onFragment: (text: string) => void,
): Promise<{ text: string; grounded: boolean }> {
  const res = await fetch(`${API_BASE}/api/conversations/${conversationId}/messages`, {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-User-Id": DEV_USER_ID,
      ...csrfHeaders("POST"),
    },
    body: JSON.stringify({ question }),
  });

  if (!res.ok) {
    const body = await res.text().catch(() => "");
    let detail = body;
    try {
      const parsed = JSON.parse(body);
      if (typeof parsed?.detail === "string") detail = parsed.detail;
    } catch {
      // body wasn't JSON -- fall back to the raw text
    }
    throw new Error(detail || `${res.status} ${res.statusText}`);
  }

  const contentType = res.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    // The refusal path: one fixed JSON body, nothing progressive about it.
    const payload = (await res.json()) as MessageSummary;
    onFragment(payload.text);
    return { text: payload.text, grounded: payload.grounded };
  }

  if (!res.body) {
    // No streaming support in this environment -- fall back to reading
    // the whole body at once rather than failing outright.
    const text = await res.text();
    onFragment(text);
    return { text, grounded: true };
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let fullText = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    const fragment = decoder.decode(value, { stream: true });
    if (fragment) {
      fullText += fragment;
      onFragment(fragment);
    }
  }
  return { text: fullText, grounded: true };
}
