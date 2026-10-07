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
