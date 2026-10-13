import { useEffect, useState } from "react";
import { type ExtractedText, getExtractedText } from "../lib/api";
// Explicit import of the shared .empty-hint/.loading-hint/.error-hint
// primitives this component relies on (Requirement 13.1).
import "../App.css";
import "./ExtractedTextPanel.css";

export function ExtractedTextPanel({ documentId }: { documentId: string | null }) {
  const [data, setData] = useState<ExtractedText | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!documentId) {
      setData(null);
      return;
    }
    setError(null);
    setData(null);
    getExtractedText(documentId)
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : String(err)));
  }, [documentId]);

  if (!documentId) {
    return (
      <p className="empty-hint">Select a document on the left once it has finished parsing to see the extracted text.</p>
    );
  }

  if (error) {
    return <p className="error-hint">{error}</p>;
  }

  if (!data) {
    return <p className="loading-hint">Loading…</p>;
  }

  return (
    <div className="extracted">
      <p className="eyebrow">
        Extracted text · {data.pages.length} page{data.pages.length === 1 ? "" : "s"} · {data.blocks.length} blocks
      </p>
      {data.blocks.map((block, i) => (
        <div key={i} className={`block ${block.kind}`}>
          {block.heading_path && block.kind !== "heading" && (
            <div className="heading-path">{block.heading_path}</div>
          )}
          <div className="block-text">{block.text}</div>
          <div className="block-meta">
            page {block.page_no} · [{block.bbox.map((n) => n.toFixed(0)).join(", ")}]
          </div>
        </div>
      ))}
      {data.blocks.length === 0 && <p className="empty-hint">No text extracted for this document.</p>}
    </div>
  );
}
