# Overview

This document explains what cmem-convo does for its users, how a request flows
through the system, and the HTTP API the frontend uses. For engineering rationale
and trade-offs, see [`TECHNICAL_BRIEF.md`](./TECHNICAL_BRIEF.md).

## What the site does

cmem-convo answers questions about an LA city council member's legislative record
in plain language, citing the actual council-file documents the answer comes from.
The goal is a low-friction, action-oriented way for a resident to understand what
their representative has done — without reading hundreds of municipal PDFs.

### The resident's experience

1. **Pick a council member.** The header shows a tab/dropdown of indexed districts
   (e.g. "Councilmember Hernandez – CD1"). Members that aren't indexed yet are
   shown but disabled.
2. **Start from a suggestion or type a question.** Each member has
   AI-generated **starter questions**, grouped by topic (housing, homelessness,
   public safety, etc.), surfaced when a chat is empty. These lower the blank-page
   barrier and are themselves derived only from documents that can actually answer
   them.
3. **Read a grounded answer.** The answer is at most ~3 short paragraphs, with
   inline markdown links like `[Food Resources Motion](…)` that open the source PDF
   on `cityclerk.lacity.org`. A **Sources** box below lists the cited documents with
   short titles. If the documents can't answer the question, the model is instructed
   to say "I don't know based on these documents" and suggest which kind of council
   file might have the answer.
4. **Continue the conversation.** Three follow-up questions are suggested after each
   answer. Follow-ups like "say more" still retrieve correctly because the backend
   rewrites them into standalone search queries using the conversation history.
5. **Browse history.** A sidebar lists prior sessions (scoped to an anonymous
   `client_id` in `localStorage`); selecting one restores the conversation and
   switches to the district it belongs to.

### How a member gets added (seeding)

A member is seeded by uploading their council activity PDF through the "+ Add
Member" UI (or `POST /api/members`):

1. The PDF is parsed with `pdfplumber` to extract council file IDs (matching
   `\d{2}-\d{4}` with optional `-S#` suffix) and the title text following each ID.
2. The member and its file list are stored in the `members` SQL table.
3. A background task streams each council file's ZIP from `scrape-cf.vercel.app`,
   extracts and parses the PDFs **in memory**, chunks the text (400 words, 50-word
   overlap), and embeds it into the member's ChromaDB collection. Already-indexed
   files are skipped, so a re-run resumes rather than duplicates.
4. After indexing, Claude Haiku generates the member's subtitle, an internal
   "context" description used in the system prompt, and the topic-grouped starter
   questions, persisted to `legislation_meta.json`.

Seeding is a background job; the client polls `GET /api/members/{id}/status` for
progress ("Indexing 14/307 council files…" → "Ready — N chunks indexed").

## Request flow for a chat message

```
Frontend (App.tsx)
   │  POST /api/chat {question, member_id, session_id, client_id}
   ▼
main.chat()                         validate member exists + is indexed
   │
   ▼
rag.answer_question()
   │  1. load recent history (last 5 turns)
   │  2. if follow-up → llm.contextualize_question() rewrites to standalone query
   │  3. query ChromaDB collection(s) for top-K chunks
   │  4. carry prior turn's chunks forward + dedup (per-session memory)
   │  5. llm.get_response() → Claude Haiku / GPT-4o-mini → JSON
   │  6. normalise sources to {title, url}; rewrite inline link hrefs to real URLs
   │  7. save exchange to history; cache first-turn answers
   ▼
ChatResponse {answer, sources[], followups[]}
   │
   ▼
Frontend renders answer (react-markdown), Sources box, follow-up buttons
```

## API reference

All endpoints are under `/api`. The backend enforces per-IP rate limits (via
`slowapi`); the limit for each route is noted below. Rate-limit headers are
returned on every response.

### Chat

**`POST /api/chat`** — ask a question. *Limit: 20/min, 300/day.*

Request:
```json
{
  "question": "What has she done about homelessness?",
  "member_id": "cd1",
  "session_id": "uuid",          // optional; enables history + follow-ups
  "client_id": "uuid",           // optional; scopes session list to a browser
  "from_starter": false,         // optional; logged for validation metrics
  "starter_topic": null          // optional; the topic a starter came from
}
```

Response:
```json
{
  "answer": "Plain-language answer with inline [Title](url) links…",
  "sources": [{ "title": "Food Resources Motion", "url": "https://cityclerk.lacity.org/onlinedocs/2025/CF-25-0381.pdf" }],
  "followups": ["Question 1?", "Question 2?", "Question 3?"]
}
```

Returns `404` if the member doesn't exist, `400` if it isn't indexed yet or the
question is empty.

### Members

| Method & path | Limit | Purpose |
|---|---|---|
| `GET /api/members` | default | List all members with `indexed`, `subtitle`, `starters`, `topic_starters`. |
| `GET /api/members/{id}` | default | One member with its file list and metadata. |
| `POST /api/members` | 3/hour | Seed a new member. Multipart form: `member_id`, `name`, `district`, `pdf`. Returns `{job_id, member_id}`. |
| `GET /api/members/{id}/status` | default | Poll the most recent seed job: `status` ∈ `parsing\|indexing\|done\|error`, plus a human `message`. |
| `POST /api/members/{id}/reseed` | 3/hour | Re-index using the file IDs already stored (no PDF upload needed). |
| `DELETE /api/members/{id}` | default | Remove the member and its ChromaDB collection. |

`member_id` must be lowercase alphanumeric with `-`/`_`. A second seed/reseed for a
member that's already running returns `409`.

### Sessions (chat history)

| Method & path | Purpose |
|---|---|
| `GET /api/sessions?client_id=…` | List sessions, newest first, optionally scoped to a browser's `client_id`. |
| `GET /api/sessions/{id}/messages` | Full message list for a session, with per-message `sources` and `followups`. |
| `DELETE /api/sessions/{id}` | Delete a session and its messages. |

### Operational

| Method & path | Purpose |
|---|---|
| `GET /api/health` | Liveness + count of indexed collections (rate-limit exempt; Railway health check). |
| `GET /api/stats/starters` | Validation metric: total exchanges, how many originated from a starter, the conversion rate, and a breakdown by topic. |

## Data the app stores

| What | Where |
|---|---|
| Vector embeddings | ChromaDB on disk (`DB_PATH`), one collection per member: `leg_{member_id}` |
| Chat messages | SQLite (`chat_history.db`) or Postgres, via LangChain's `SQLChatMessageHistory` |
| Sources/followups per exchange | `message_sources` SQL table (also logs `from_starter`/`starter_topic`) |
| Member registry | `members` SQL table |
| Member metadata (subtitle, starters, context) | `legislation_meta.json`, with a code fallback (`_SEEDS`) |

The source documents themselves are **not** stored — they're streamed, parsed, and
discarded during indexing; only the chunked text + embeddings are kept.
