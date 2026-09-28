import { useState } from "react";
import { DocumentList } from "./components/DocumentList";
import { ExtractedTextPanel } from "./components/ExtractedTextPanel";
import "./App.css";

export default function App() {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  
  return (
    <div>
      <header className="appbar">
        <div className="mark" />
        <div className="brand">Deal Workspace</div>
        <div className="who">
          <div className="avatar">DV</div>
          dev@example.com
        </div>
      </header>

      <div className="shell">
        <aside className="rail">
          <p className="eyebrow">Projects</p>
          <button className="proj" aria-current="true">
            <div className="nm">Development Project</div>
          </button>
        </aside>

        <section className="mid">
          <DocumentList selectedId={selectedId} onSelect={setSelectedId} />
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
      </div>
    </div>
  );
}
