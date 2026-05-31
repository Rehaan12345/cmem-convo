import { useState, useEffect } from "react";

interface CouncilFile {
  id: string;
  title: string;
}

interface Props {
  apiUrl: string;
  memberId: string;
  memberName: string;
  onClose: () => void;
}

export default function FilesModal({ apiUrl, memberId, memberName, onClose }: Props) {
  const [files, setFiles] = useState<CouncilFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [search, setSearch] = useState("");

  useEffect(() => {
    async function load() {
      try {
        const res = await fetch(`${apiUrl}/api/members/${memberId}`);
        if (!res.ok) throw new Error("Failed to load member data");
        const data = await res.json();
        setFiles(data.files ?? []);
      } catch (e) {
        setError("Could not load file list.");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, [apiUrl, memberId]);

  const filtered = search.trim()
    ? files.filter(
        (f) =>
          f.id.toLowerCase().includes(search.toLowerCase()) ||
          f.title.toLowerCase().includes(search.toLowerCase())
      )
    : files;

  function handleBackdropClick(e: React.MouseEvent<HTMLDivElement>) {
    if (e.target === e.currentTarget) onClose();
  }

  function cfmUrl(fileId: string) {
    return `https://cityclerk.lacity.org/lacityclerkconnect/index.cfm?fa=ccfi.viewrecord&cfnumber=${fileId}`;
  }

  return (
    <div className="modal-backdrop" onClick={handleBackdropClick}>
      <div className="modal-box files-modal-box">
        <div className="modal-header">
          <div>
            <h2 className="modal-title">Indexed Council Files</h2>
            <p className="files-modal-subtitle">{memberName}</p>
          </div>
          <button className="modal-close-btn" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="files-modal-search-row">
          <input
            className="files-modal-search"
            placeholder="Search files…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            autoFocus
          />
          {!loading && (
            <span className="files-modal-count">
              {filtered.length}{search ? ` of ${files.length}` : ""} file{files.length !== 1 ? "s" : ""}
            </span>
          )}
        </div>

        <div className="files-modal-list">
          {loading && <p className="files-modal-status">Loading…</p>}
          {error && <p className="files-modal-status files-modal-status--error">{error}</p>}
          {!loading && !error && filtered.length === 0 && (
            <p className="files-modal-status">No files match.</p>
          )}
          {filtered.map((f) => (
            <a
              key={f.id}
              href={cfmUrl(f.id)}
              target="_blank"
              rel="noopener noreferrer"
              className="files-modal-row"
            >
              <span className="files-modal-id">{f.id}</span>
              {f.title && <span className="files-modal-title">{f.title}</span>}
              <span className="files-modal-arrow">↗</span>
            </a>
          ))}
        </div>
      </div>
    </div>
  );
}
