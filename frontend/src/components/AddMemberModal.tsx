import { useState, useRef, useEffect } from "react";

interface Props {
  apiUrl: string;
  onClose: () => void;
  onDone: () => void;
}

type SeedStatus = "idle" | "uploading" | "parsing" | "indexing" | "done" | "error";

export default function AddMemberModal({ apiUrl, onClose, onDone }: Props) {
  const [memberId, setMemberId] = useState("");
  const [name, setName] = useState("");
  const [district, setDistrict] = useState("");
  const [pdf, setPdf] = useState<File | null>(null);
  const [error, setError] = useState("");
  const [status, setStatus] = useState<SeedStatus>("idle");
  const [statusMsg, setStatusMsg] = useState("");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => () => {
    if (pollRef.current) clearInterval(pollRef.current);
  }, []);

  function handleBackdropClick(e: React.MouseEvent<HTMLDivElement>) {
    if (e.target === e.currentTarget && status !== "uploading" && status !== "parsing" && status !== "indexing") {
      onClose();
    }
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError("");

    const id = memberId.trim().toLowerCase();
    if (!id || !/^[a-z0-9_-]+$/.test(id)) {
      setError("Member ID must be lowercase letters, numbers, hyphens, or underscores (e.g. cd1)");
      return;
    }
    if (!name.trim()) { setError("Name is required"); return; }
    if (!district.trim()) { setError("District is required"); return; }
    if (!pdf) { setError("PDF file is required"); return; }

    setStatus("uploading");
    setStatusMsg("Uploading PDF…");

    const form = new FormData();
    form.append("member_id", id);
    form.append("name", name.trim());
    form.append("district", district.trim());
    form.append("pdf", pdf);

    try {
      const res = await fetch(`${apiUrl}/api/members`, { method: "POST", body: form });
      if (!res.ok) {
        const body = await res.json();
        setError(body.detail || "Failed to start seeding");
        setStatus("idle");
        return;
      }
      const { job_id } = await res.json();
      startPolling(id, job_id);
    } catch {
      setError("Could not reach the server");
      setStatus("idle");
    }
  }

  function startPolling(id: string, _jobId: string) {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const res = await fetch(`${apiUrl}/api/members/${id}/status`);
        if (!res.ok) return;
        const job = await res.json();
        setStatus(job.status as SeedStatus);
        setStatusMsg(job.message);
        if (job.status === "done") {
          clearInterval(pollRef.current!);
          pollRef.current = null;
          setTimeout(onDone, 1500);
        } else if (job.status === "error") {
          clearInterval(pollRef.current!);
          pollRef.current = null;
          setError(job.message);
        }
      } catch { /* keep polling */ }
    }, 2000);
  }

  const busy = status === "uploading" || status === "parsing" || status === "indexing";
  const progressLabel =
    status === "uploading" ? "Uploading…" :
    status === "parsing"  ? "Parsing PDF…" :
    status === "indexing" ? "Indexing council files…" :
    status === "done"     ? "Done!" : "";

  return (
    <div className="modal-backdrop" onClick={handleBackdropClick}>
      <div className="modal-box">
        <div className="modal-header">
          <h2 className="modal-title">Add Council Member</h2>
          {!busy && (
            <button className="modal-close-btn" onClick={onClose} aria-label="Close">✕</button>
          )}
        </div>

        {status === "idle" || error ? (
          <form className="modal-form" onSubmit={handleSubmit}>
            <label className="modal-label">
              Member ID
              <input
                className="modal-input"
                placeholder="e.g. cd1"
                value={memberId}
                onChange={(e) => setMemberId(e.target.value)}
                disabled={busy}
              />
            </label>
            <label className="modal-label">
              Name
              <input
                className="modal-input"
                placeholder="e.g. Eunisses Hernandez"
                value={name}
                onChange={(e) => setName(e.target.value)}
                disabled={busy}
              />
            </label>
            <label className="modal-label">
              District
              <input
                className="modal-input"
                placeholder="e.g. Council District 1"
                value={district}
                onChange={(e) => setDistrict(e.target.value)}
                disabled={busy}
              />
            </label>
            <label className="modal-label">
              Council File List PDF
              <div
                className="modal-file-drop"
                onClick={() => fileInputRef.current?.click()}
              >
                {pdf ? (
                  <span className="modal-file-name">{pdf.name}</span>
                ) : (
                  <span className="modal-file-hint">Click to select PDF</span>
                )}
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".pdf"
                  style={{ display: "none" }}
                  onChange={(e) => setPdf(e.target.files?.[0] ?? null)}
                />
              </div>
            </label>

            {error && <p className="modal-error">{error}</p>}

            <button className="modal-submit-btn" type="submit" disabled={busy}>
              Start Seeding
            </button>
          </form>
        ) : (
          <div className="modal-progress">
            <p className="modal-progress-label">{progressLabel}</p>
            <div className="modal-progress-bar">
              <div
                className={`modal-progress-fill modal-progress-fill--${status}`}
              />
            </div>
            <p className="modal-progress-msg">{statusMsg}</p>
          </div>
        )}
      </div>
    </div>
  );
}
