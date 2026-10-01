import { useEffect, useRef, useState } from "react";
import { api, type CitationDetail, type TokenProvider } from "../api";

export function CitationPanel({
  citation,
  error,
  onClose,
  tokens,
}: {
  citation: CitationDetail | null;
  error: string | null;
  onClose: () => void;
  tokens: TokenProvider;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  async function download(c: CitationDetail) {
    setDownloadError(null);
    try {
      const blob = await api.download(tokens, c.download_url);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = c.title;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      setDownloadError("Download failed or access was removed.");
    }
  }
  useEffect(() => {
    if (citation) heading.current?.focus();
  }, [citation]);

  return (
    <aside className="citation-panel" aria-label="Source viewer" aria-live="polite">
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {!citation && !error && <p className="muted">Select a citation to view the source passage.</p>}
      {citation && (
        <article>
          <h2 tabIndex={-1} ref={heading}>
            {citation.title}
          </h2>
          <p className="muted small">
            {citation.section} · {citation.location} · revision {citation.document_version} · label{" "}
            {citation.sensitivity_label}
          </p>
          <pre className="passage">{citation.text}</pre>
          <p className="muted small">
            Source updated {citation.source_modified_at ? new Date(citation.source_modified_at).toLocaleString() : "unknown"}
            {" · "}permissions synced {citation.acl_synced_at ? new Date(citation.acl_synced_at).toLocaleString() : "unknown"}
          </p>
          {downloadError && (
            <p role="alert" className="error small">
              {downloadError}
            </p>
          )}
          <div className="row">
            <button type="button" className="link" onClick={() => void download(citation)}>
              Download original
            </button>
            <button type="button" className="link" onClick={onClose}>
              Close
            </button>
          </div>
        </article>
      )}
    </aside>
  );
}
