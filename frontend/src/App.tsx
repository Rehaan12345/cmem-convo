import { useState, useRef, useEffect, useCallback } from "react";
import Message, { type MessageData } from "./components/Message";
import StarterQuestions from "./components/StarterQuestions";
import Sidebar, { type Session } from "./components/Sidebar";
import AddMemberModal from "./components/AddMemberModal";
import FilesModal from "./components/FilesModal";
import "./App.css";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

interface Member {
  id: string;
  name: string;
  district: string;
  indexed: boolean;
  starters?: string[];
  topic_starters?: Record<string, string[]>;
  subtitle?: string;
}

export default function App() {
  const [members, setMembers] = useState<Member[]>([]);
  const [activeMember, setActiveMember] = useState<Member | null>(null);
  const [messages, setMessages] = useState<MessageData[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [clientId] = useState<string>(() => {
    const stored = localStorage.getItem("cmem_client_id");
    if (stored) return stored;
    const id = crypto.randomUUID();
    localStorage.setItem("cmem_client_id", id);
    return id;
  });
  const [sessionId, setSessionId] = useState<string>(() => crypto.randomUUID());

  const [sessions, setSessions] = useState<Session[]>([]);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [showAddModal, setShowAddModal] = useState(false);
  const [showFilesModal, setShowFilesModal] = useState(false);

  const bottomRef = useRef<HTMLDivElement>(null);

  const fetchSessions = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/sessions?client_id=${clientId}`);
      if (!res.ok) return;
      setSessions(await res.json());
    } catch { /* silent */ }
  }, [clientId]);

  const fetchMembers = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/members`);
      if (!res.ok) return;
      const data: Member[] = await res.json();
      setMembers(data);
      setActiveMember((prev) => {
        if (prev) {
          const updated = data.find((m) => m.id === prev.id);
          return updated ?? prev;
        }
        return data.find((m) => m.indexed) ?? null;
      });
    } catch { /* backend not running yet */ }
  }, []);

  useEffect(() => {
    fetchMembers();
    fetchSessions();
  }, [fetchMembers, fetchSessions]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  function selectMember(member: Member) {
    if (loading || !member.indexed) return;
    if (activeMember?.id === member.id) return;
    setActiveMember(member);
    setMessages([]);
    setInput("");
    setSessionId(crypto.randomUUID());
  }

  async function sendQuestion(question: string, fromStarter = false, topic?: string) {
    if (!question.trim() || loading || !activeMember) return;
    setMessages((prev) => [...prev, { role: "user", text: question }]);
    setInput("");
    setLoading(true);
    try {
      const res = await fetch(`${API_URL}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question,
          member_id: activeMember.id,
          session_id: sessionId,
          client_id: clientId,
          from_starter: fromStarter,
          starter_topic: topic ?? null,
        }),
      });
      if (!res.ok) throw new Error(`Server error: ${res.status}`);
      const data = await res.json();
      setMessages((prev) => [
        ...prev,
        { role: "assistant", text: data.answer, sources: data.sources, followups: data.followups },
      ]);
      fetchSessions();
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", text: "Sorry, something went wrong connecting to the server. Is the backend running?" },
      ]);
    } finally {
      setLoading(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendQuestion(input);
    }
  }

  function handleNewChat() {
    if (loading) return;
    setMessages([]);
    setInput("");
    setSessionId(crypto.randomUUID());
  }

  async function handleSelectSession(session: Session) {
    if (loading) return;
    setSidebarOpen(false);
    try {
      const res = await fetch(`${API_URL}/api/sessions/${session.session_id}/messages`);
      if (!res.ok) return;
      const data = await res.json();
      const loaded: MessageData[] = data.messages.map((m: { role: string; content: string; sources?: MessageData["sources"]; followups?: string[] }) => ({
        role: m.role as "user" | "assistant",
        text: m.content,
        sources: m.sources,
        followups: m.followups,
      }));
      // Chat history takes precedence over the top-nav selection: switch the
      // active district to the one this conversation belongs to. If that
      // district is gone or unindexed, clear the selection (input stays disabled).
      const legId = session.legislation_ids[0];
      const sessionMember = legId ? members.find((m) => m.id === legId && m.indexed) ?? null : null;
      setActiveMember(sessionMember);
      setMessages(loaded);
      setSessionId(session.session_id);
      setInput("");
    } catch { /* silent */ }
  }

  async function handleDeleteSession(sid: string) {
    try {
      await fetch(`${API_URL}/api/sessions/${sid}`, { method: "DELETE" });
      fetchSessions();
    } catch { /* silent */ }
  }

  const showWelcome = messages.length === 0 && !loading;
  const showStarters = !loading && (
    !!activeMember?.starters?.length ||
    !!(activeMember?.topic_starters && Object.keys(activeMember.topic_starters).length)
  );

  return (
    <div className="app">
      <Sidebar
        sessions={sessions}
        activeSessionId={sessionId}
        loading={loading}
        isOpen={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onNewChat={handleNewChat}
        onSelectSession={handleSelectSession}
        onDeleteSession={handleDeleteSession}
      />

      <div className="main-panel">
        <header className="app-header">
          <div className="header-inner">
            <div className="header-top">
              <button
                className="sidebar-toggle-btn"
                onClick={() => setSidebarOpen((o) => !o)}
                aria-label="Toggle history"
              >
                ☰
              </button>
              <div className="header-text">
                <h1 className="header-title">
                  {activeMember ? activeMember.name : "cmem-convo"}
                </h1>
                <p className="header-subtitle">
                  {activeMember
                    ? `${activeMember.district} · Los Angeles`
                    : "Select a council member below"}
                </p>
              </div>
              <div className="header-right">
                {activeMember && (
                  <button
                    className="view-files-btn"
                    onClick={() => setShowFilesModal(true)}
                    title="View indexed council files"
                  >
                    View files
                  </button>
                )}
                <button
                  className="add-member-btn"
                  onClick={() => setShowAddModal(true)}
                  title="Add council member"
                >
                  + Add Member
                </button>
              </div>
            </div>

            {members.length > 0 && (
              <div className="member-selector">
                <div className="member-selector-buttons">
                  {members.map((m) => (
                    <button
                      key={m.id}
                      className={`member-tab${activeMember?.id === m.id ? " member-tab--active" : ""}${!m.indexed ? " member-tab--unindexed" : ""}`}
                      onClick={() => selectMember(m)}
                      disabled={loading || !m.indexed}
                      title={m.indexed ? m.district : "Not yet indexed"}
                    >
                      <span className="member-tab-label">
                        <span className="member-tab-label-name">Councilmember {m.name} - </span>
                        <span className="member-tab-label-id">{m.id.toUpperCase()}</span>
                      </span>
                    </button>
                  ))}
                </div>
                <select
                  className="member-selector-dropdown"
                  value={activeMember?.id ?? ""}
                  onChange={(e) => {
                    const m = members.find((mb) => mb.id === e.target.value);
                    if (m) selectMember(m);
                  }}
                  disabled={loading}
                >
                  <option value="" disabled>Select district</option>
                  {members.map((m) => (
                    <option key={m.id} value={m.id} disabled={!m.indexed}>
                      {m.indexed
                        ? `Councilmember ${m.name} - ${m.id.toUpperCase()}`
                        : `${m.id.toUpperCase()} (not indexed)`}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>
        </header>

        <main className="chat-area">
          <div className="chat-inner">
            {showWelcome && activeMember && (
              <div className="welcome">
                <p className="welcome-text">
                  Ask me anything about {activeMember.name}'s legislative record in {activeMember.district}.
                  I'll answer in plain language and cite which document my answer comes from.
                </p>
              </div>
            )}

            {showWelcome && !activeMember && members.length === 0 && (
              <div className="welcome">
                <p className="welcome-text">
                  No council members indexed yet. Click "+ Add Member" to seed a council district.
                </p>
              </div>
            )}

            {messages.map((msg, i) => (
              <Message key={i} message={msg} onFollowup={sendQuestion} />
            ))}

            {loading && (
              <div className="message message--assistant">
                <div className="message-bubble loading-bubble">
                  <span className="dot" /><span className="dot" /><span className="dot" />
                </div>
              </div>
            )}

            {showStarters && (
              <StarterQuestions
                starters={activeMember?.starters}
                topicStarters={activeMember?.topic_starters}
                onSelect={sendQuestion}
              />
            )}
            <div ref={bottomRef} />
          </div>
        </main>

        <footer className="input-area">
          <div className="input-inner">
            <textarea
              className="chat-input"
              rows={1}
              placeholder={
                activeMember
                  ? `Ask about ${activeMember.name}… (Enter to send)`
                  : "Select a council member to start"
              }
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={loading || !activeMember}
            />
            <button
              className="send-btn"
              onClick={() => sendQuestion(input)}
              disabled={loading || !input.trim() || !activeMember}
              aria-label="Send"
            >
              ↑
            </button>
          </div>
        </footer>
      </div>

      {showAddModal && (
        <AddMemberModal
          apiUrl={API_URL}
          onClose={() => setShowAddModal(false)}
          onDone={() => {
            setShowAddModal(false);
            fetchMembers();
          }}
        />
      )}

      {showFilesModal && activeMember && (
        <FilesModal
          apiUrl={API_URL}
          memberId={activeMember.id}
          memberName={activeMember.name}
          onClose={() => setShowFilesModal(false)}
        />
      )}
    </div>
  );
}
