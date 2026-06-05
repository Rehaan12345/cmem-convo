import ReactMarkdown from "react-markdown";
import "./Message.css";

type SourceRef = { title: string; url: string } | string;

interface MessageData {
  role: "user" | "assistant";
  text: string;
  sources?: SourceRef[];
  followups?: string[];
}

interface Props {
  message: MessageData;
  onFollowup: (q: string) => void;
}

export default function Message({ message, onFollowup }: Props) {
  const isUser = message.role === "user";

  return (
    <div className={`message ${isUser ? "message--user" : "message--assistant"}`}>
      <div className="message-bubble">
        {isUser ? (
          <p className="message-text">{message.text}</p>
        ) : (
          <div className="message-text">
            <ReactMarkdown
              components={{
                a: (props) => <a {...props} target="_blank" rel="noopener noreferrer" />,
              }}
            >
              {message.text}
            </ReactMarkdown>
          </div>
        )}

        {!isUser && message.sources && message.sources.length > 0 && (
          <div className="message-sources">
            <span className="sources-label">Sources:</span>
            {message.sources.map((s) => {
              if (typeof s === "string") {
                const isUrl = s.startsWith("http");
                const label = isUrl ? s.split("/").pop() || s : s;
                return isUrl ? (
                  <a key={s} href={s} target="_blank" rel="noopener noreferrer" className="source-tag source-tag--link">
                    {label}
                  </a>
                ) : (
                  <span key={s} className="source-tag">{s}</span>
                );
              }
              return (
                <a key={s.url} href={s.url} target="_blank" rel="noopener noreferrer" className="source-tag source-tag--link">
                  {s.title}
                </a>
              );
            })}
          </div>
        )}

        {!isUser && message.followups && message.followups.length > 0 && (
          <div className="message-followups">
            <p className="followups-label">You might also ask:</p>
            <div className="followups-list">
              {message.followups.map((q) => (
                <button key={q} className="followup-chip" onClick={() => onFollowup(q)}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export type { MessageData };
