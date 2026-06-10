# cmem-convo

A RAG chat app that lets Los Angeles residents ask plain-language questions about
their city council member's legislative record. Pick a council district, ask a
question, and get a concise answer with inline links to the source documents and
three suggested follow-ups.

The corpus for each member is built from their public council files, scraped from
`cityclerk.lacity.org`, embedded locally, and stored in a per-member vector
collection. Answers are grounded **only** in those documents — the model is
instructed not to use outside knowledge.

- **Backend:** FastAPI + ChromaDB, deployed on Railway.
- **Frontend:** Vite/React, served as a static build.
- **LLM:** Claude Haiku by default (`claude-haiku-4-5`), or GPT-4o-mini.

For a deeper tour of features and the API, see [`OVERVIEW.md`](./OVERVIEW.md).
For the engineering decisions and trade-offs, see [`TECHNICAL_BRIEF.md`](./TECHNICAL_BRIEF.md).

## How it works (60 seconds)

1. **Seed a member** — upload a council activity PDF. The backend parses council
   file IDs (`\d{2}-\d{4}`) out of it, streams each file's ZIP from a scraper
   service, extracts the PDFs in memory, chunks the text, and writes embeddings to
   a ChromaDB collection (`leg_{member_id}`).
2. **Ask a question** — the backend retrieves the top-K relevant chunks, sends them
   plus the question to the LLM, and gets back a JSON answer with inline citations
   and follow-ups.
3. **Read the answer** — the frontend renders the answer with clickable source
   links and a sources box, plus follow-up suggestions you can click to continue.

## Running locally

You need an `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY` if using OpenAI). The backend
`.env` lives in `backend/`. Copy the example and fill it in:

```bash
cp .env.example backend/.env   # then edit backend/.env
```

### Backend

```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

On first run, sentence-transformers downloads the `all-MiniLM-L6-v2` embedding
model (~80 MB). The API is then at `http://localhost:8000`; health check at
`/api/health`.

### Frontend

```bash
cd frontend
npm install
npm run dev          # Vite dev server on http://localhost:5173
npm run build        # production build → dist/
npx tsc --noEmit     # type-check only
```

The frontend talks to the backend at `VITE_API_URL` (defaults to
`http://localhost:8000`).

### Docker (full stack)

```bash
docker-compose up --build
```

This builds the backend image (pre-baking the embedding model so the container
starts without a network download) and mounts a single named volume at `/app/data`
for ChromaDB, chat history, and member metadata.

## Configuration

All backend config is via environment variables (in `backend/.env`):

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | Required for Claude (the default provider). |
| `OPENAI_API_KEY` | — | Required only if `LLM_PROVIDER=openai`. |
| `LLM_PROVIDER` | `claude` | `claude` or `openai`. |
| `ALLOWED_ORIGINS` | localhost:5173 | Comma-separated CORS origins. |
| `DATABASE_URL` | — | Postgres URL (Railway). Falls back to SQLite if unset. |
| `DB_PATH` | `backend/chroma_db` | ChromaDB on-disk path. |
| `HISTORY_DB_PATH` | `backend/chat_history.db` | SQLite path (ignored if `DATABASE_URL` set). |
| `META_PATH` | `backend/legislation_meta.json` | Member metadata file. |

## Deployment

- **Backend** deploys to Railway from `backend/Dockerfile` (see `railway.toml`).
  Health check at `/api/health`. Railway provisions Postgres via `DATABASE_URL`.
- **Frontend** is a static Vite build; deploy `dist/` to any static host with
  `VITE_API_URL` pointed at the backend.

## Project layout

```
backend/
  main.py              FastAPI app, all endpoints, background seed task
  rag.py               Retrieval orchestration, caching, source normalisation
  llm.py               Claude + OpenAI calls, system prompt, JSON parsing
  ingest.py            Chunking + ChromaDB writes
  downloader.py        Streams council-file ZIPs, parses PDFs in memory
  history.py           SQL engine, chat history, sessions, message_sources
  member_registry.py   members table CRUD
  legislation_meta.py  Generates + persists member subtitle/starters
frontend/
  src/App.tsx          Main chat UI and state
  src/components/       Message, Sidebar, StarterQuestions, modals
```

> **Status:** single-process by design. The answer cache, follow-up chunk
> carry-forward, and seed-job tracking all live in process memory, and ChromaDB is
> on local disk. Run one worker. See `TECHNICAL_BRIEF.md` and `CLAUDE.md` for the
> scaling caveats before relying on horizontal scale.
