import { useCallback, useEffect, useRef, useState } from "react";
import {
  type DocumentSummary,
  completeUpload,
  createUploadSlot,
  getDocument,
  listDocuments,
  uploadToS3,
} from "../lib/api";
import "./DocumentList.css";

const PROCESSING_STATUSES = new Set(["awaiting_upload", "pending_parse", "parsing", "indexing"]);

function statusLabel(status: string): string {
  switch (status) {
    case "pending_parse":
      return "Queued";
    case "parsing":
      return "Extracting text";
    case "indexing":
      return "Indexing";
    case "ready":
      return "Ready";
    case "failed":
      return "Failed";
    default:
      return status;
  }
}

interface DocumentListProps {
  selectedId: string | null;
  onSelect: (documentId: string) => void;
}

export function DocumentList({ selectedId, onSelect }: DocumentListProps) {
  const [docs, setDocs] = useState<DocumentSummary[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const pollHandles = useRef<Map<string, number>>(new Map());

  const refresh = useCallback(async () => {
    const list = await listDocuments();
    setDocs(list);
  }, []);

  useEffect(() => {
    refresh().catch((err) => setError(String(err)));
  }, [refresh]);

  const pollDocument = useCallback((documentId: string) => {
    if (pollHandles.current.has(documentId)) return;
    const handle = window.setInterval(async () => {
      try {
        const doc = await getDocument(documentId);
        setDocs((prev) => {
          const next = prev.filter((d) => d.id !== documentId);
          return [doc, ...next];
        });
        if (!PROCESSING_STATUSES.has(doc.status)) {
          window.clearInterval(handle);
          pollHandles.current.delete(documentId);
        }
      } catch (err) {
        window.clearInterval(handle);
        pollHandles.current.delete(documentId);
        setError(String(err));
      }
    }, 2000);
    pollHandles.current.set(documentId, handle);
  }, []);

  useEffect(() => {
    return () => {
      pollHandles.current.forEach((handle) => window.clearInterval(handle));
    };
  }, []);

  const handleFileChosen = useCallback(
    async (file: File) => {
      setError(null);
      setUploading(true);
      try {
        const slot = await createUploadSlot(file);
        await uploadToS3(slot.upload_url, file);
        const result = await completeUpload(slot.document_id);
        await refresh();
        pollDocument(result.document_id);
      } catch (err) {
        setError(String(err));
      } finally {
        setUploading(false);
      }
    },
    [refresh, pollDocument],
  );

  return (
    <div>
      <p className="eyebrow">Documents · {docs.length}</p>
      <div id="docList">
        {docs.map((d) => (
          <button
            key={d.id}
            className={`doc ${PROCESSING_STATUSES.has(d.status) ? "proc" : ""} ${d.status === "failed" ? "failed" : ""} ${d.id === selectedId ? "selected" : ""}`}
            onClick={() => onSelect(d.id)}
          >
            <span className="dot" />
            <span className="nm">{d.filename}</span>
            <span className="st">{statusLabel(d.status)}</span>
          </button>
        ))}
        {docs.length === 0 && <p className="empty-hint">No documents yet. Add one to see it get parsed.</p>}
      </div>

      <input
        ref={fileInputRef}
        type="file"
        accept="application/pdf"
        style={{ display: "none" }}
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) handleFileChosen(file);
          e.target.value = "";
        }}
      />
      <button className="primary" disabled={uploading} onClick={() => fileInputRef.current?.click()}>
        {uploading ? "Uploading…" : "+ Add document"}
      </button>
      {error && <p className="error-hint">{error}</p>}
    </div>
  );
}
