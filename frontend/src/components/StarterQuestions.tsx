import { useState } from "react";
import "./StarterQuestions.css";

const DEFAULT_STARTERS = [
  "What is this legislation about?",
  "Who introduced this and when?",
  "What does this change or authorize?",
  "How does this affect residents?",
];

const ALL = "All";

interface Props {
  starters?: string[];
  topicStarters?: Record<string, string[]>;
  // fromStarter/topic are reported so the backend can log starter origination.
  onSelect: (question: string, fromStarter?: boolean, topic?: string) => void;
}

export default function StarterQuestions({ starters, topicStarters, onSelect }: Props) {
  const topics = topicStarters ? Object.keys(topicStarters) : [];
  const [selectedTopic, setSelectedTopic] = useState<string>(ALL);

  const fallback = starters && starters.length > 0 ? starters : DEFAULT_STARTERS;
  const questions =
    selectedTopic !== ALL && topicStarters ? topicStarters[selectedTopic] ?? fallback : fallback;
  // Only a real topic selection is logged; the "All" view reports no topic.
  const loggedTopic = selectedTopic === ALL ? undefined : selectedTopic;

  return (
    <div className="starters">
      {topics.length > 0 && (
        <div className="starters-category">
          <label className="starters-category-label" htmlFor="topic-select">
            Question category
          </label>
          <select
            id="topic-select"
            className={`starters-category-select${selectedTopic !== ALL ? " starters-category-select--active" : ""}`}
            value={selectedTopic}
            onChange={(e) => setSelectedTopic(e.target.value)}
          >
            <option value={ALL}>All questions</option>
            {topics.map((t) => (
              <option key={t} value={t}>{t}</option>
            ))}
          </select>
        </div>
      )}
      <div className="starters-ask">
        <p className="starters-label">Try asking:</p>
        <div className="starters-grid">
          {questions.map((q) => (
            <button
              key={q}
              className="starter-chip"
              onClick={() => onSelect(q, true, loggedTopic)}
            >
              {q}
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
