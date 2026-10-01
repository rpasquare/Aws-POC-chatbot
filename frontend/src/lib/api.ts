/** Thin client for the Deal Workspace API.
 *
 * `X-User-Id` stands in for a resolved session until real authentication
 * lands (Sprint 4). The dev user and project ids match the seed data.
 */

const API_BASE = "http://localhost:8000";
export const DEV_USER_ID = "00000000-0000-0000-0000-000000000001";
export const DEV_PROJECT_ID = "00000000-0000-0000-0000-0000000000a1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      "X-User-Id": DEV_USER_ID,
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${detail}`);
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
