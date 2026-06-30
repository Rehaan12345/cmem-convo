# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Git workflow

Never `git commit` or `git push` unless the user explicitly says to commit or push in that message. Complete the code changes, confirm they work, and stop — the user will ask separately when ready to commit or push.

## What this project is

A RAG chat app that lets LA residents ask plain-language questions about their city council member's legislative record. Users select a council district, ask a question, and get an answer with inline source links and follow-up suggestions. Deployed on Railway (backend only); frontend is a static Vite/React build.

## Dev commands

**Backend**
```bash
cd backend
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

**Frontend**
```bash
cd frontend
npm install
npm run dev        # Vite dev server on :5173
npm run build      # production build
npx tsc --noEmit   # type-check only
```

**Docker (full stack)**
```bash
docker-compose up --build
```

The `.env` file lives in `backend/`. Required vars: `ANTHROPIC_API_KEY`, optionally `OPENAI_API_KEY`, `LLM_PROVIDER` (`claude` default or `openai`), `ALLOWED_ORIGINS`, `DATABASE_URL` (Railway Postgres; falls back to SQLite), `DB_PATH`, `HISTORY_DB_PATH`, `META_PATH`, optionally `CITYWISE_DATABASE_URL` (read-only Postgres for the `citywise_full` structured legislative DB; when unset, structured fact-card grounding is disabled and answers fall back to RAG-only).

## Architecture

### Data flow for a chat message

1. **Frontend** (`App.tsx`) POSTs `{question, member_id, session_id, client_id}` to `/api/chat`.
2. **`rag.answer_question()`** (rag.py) — the central orchestrator:
   - Loads conversation history from SQLite/Postgres via `history.load_recent()`.
   - **Structured intent routing:** `llm.classify_question_intent()` (a cheap LLM classifier) labels the question `vote_record`, `sponsored`, `nc_district`, or `none`. For `vote_record` it answers from `citywise.get_member_votes()` (full vote counts as source of truth + a capped, citable sample, NO votes first), for `sponsored` from `citywise.get_member_legislation()`, and for `nc_district` ("which neighborhood councils are in / have filed CIS in my district") from `citywise.get_ncs_in_district()` rendered via `render_nc_summary()` (the active member fixes the district; **summary-only — no per-NC document to cite**) — **all three skip vector retrieval**, since the seeded ChromaDB collection is a tiny, skewed subset. `none` falls through to RAG below. Any classifier error ⇒ `none`. Runs before the follow-up rewrite so a structured turn doesn't burn a `contextualize_question()` call.
   - If it's a follow-up, calls `llm.contextualize_question()` to rewrite the question into a standalone search query (so "say more" still retrieves relevant chunks).
   - Queries ChromaDB for top-K chunks across the member's collection.
   - Carries the prior turn's chunks forward and deduplicates (`_session_chunks` in-memory per session).
   - **Structured grounding:** extracts the council-file IDs from the retrieved chunks and calls `citywise.get_fact_cards()` (read-only `citywise_full` DB) to fetch verified type/status/sponsors/vote-tally and *this council member's own vote* per file. These are rendered as an authoritative "VERIFIED RECORD" block prepended to the document context, so the model states votes/sponsors/status from the DB instead of inferring them from PDF prose. Degrades to RAG-only if `CITYWISE_DATABASE_URL` is unset/unreachable.
   - Calls `llm.get_response()` which dispatches to Claude Haiku or GPT-4o-mini.
   - Normalises the `sources` list to `{title, url}` objects and rewrites inline markdown link hrefs from source labels → real cityclerk.lacity.org PDF URLs.
   - Saves the exchange to history; caches first-turn answers in memory.
3. **Frontend** renders the answer through `react-markdown` (links open in new tab) and the Sources box below with short titles.

### Seeding a council member

Seeding is triggered by uploading a council activity PDF through the "Add Member" UI (or `/api/members` POST). The flow:

1. The PDF is parsed with pdfplumber to extract council file IDs (`\d{2}-\d{4}`) and their titles.
2. `member_registry.upsert_member()` stores them in the `members` SQL table.
3. `_run_indexing()` (main.py background task) streams each council file ZIP from `scrape-cf.vercel.app` via `downloader.stream_and_parse()`, extracts PDFs in memory (no disk), chunks text (400 words, 50-word overlap), and adds to ChromaDB via `ingest.ingest_from_memory()`.
4. Already-indexed files are skipped (checked by `council_file` metadata in ChromaDB).
5. After indexing, `legislation_meta.generate_and_save_meta()` calls Claude Haiku to generate a subtitle, context description, and starter questions, stored in `legislation_meta.json`.

### Storage

| What | Where |
|---|---|
| Vector embeddings | ChromaDB on disk at `DB_PATH` (`chroma_db/`), one collection per member: `leg_{member_id}` |
| Chat history (messages) | SQLite (`chat_history.db`) or Postgres via LangChain's `SQLChatMessageHistory` |
| Sources/followups per exchange | `message_sources` SQL table (session_id + exchange_index) |
| Member registry | `members` SQL table (same DB as history) |
| Member metadata (subtitle, starters) | `legislation_meta.json` on disk; falls back to `_SEEDS` dict in `legislation_meta.py` |
| NC directory / NC→district / CIS engagement | Derived tables in the read-only `citywise_full` DB: `nc_directory`, `nc_council_district`, `nc_member_engagement`. Built by idempotent scripts in `backend/scripts/` (`derive_nc_districts.py`, `build_nc_directory.py`, `build_nc_engagement.py`); the app only SELECTs them via `citywise.py` |

### Neighborhood-council (NC) tables

These power the `nc_district` intent. No source carries neighborhood-council → council-district as a column, so it is **derived geographically**: `derive_nc_districts.py` (one-time, needs `shapely` — dev-only, never imported by the deployed backend) intersects the LA GeoHub certified-NC boundary polygons (ArcGIS layer 18) with council-district polygons (layer 13) and writes the checked-in `backend/nc_council_district.csv` (`nc_id, council_district, overlap_fraction, is_primary`; one-to-many — NCs can straddle districts). `build_nc_directory.py` loads that CSV plus `backend/neighborhood_councils_with_region.csv` (EmpowerLA export) into `nc_directory` + `nc_council_district`. `build_nc_engagement.py` aggregates Community Impact Statements (`file_activities_url.type_id=3`) ⋈ `project_movers` ⋈ `council_members`, parsing the submitting NC from the CIS title via `nc_names.parse_nc_name`/`normalize_nc_name` and resolving it to a canonical `nc_id` (normalized exact match → curated alias map → difflib; ~99.6% of CIS rows resolve, unmatched kept with `nc_id NULL` and written to `nc_engagement_unmatched.csv` for curation). **Note:** `SERVICE_RE` in the EmpowerLA CSV is the 12 EmpowerLA *service regions*, NOT the 15 council districts — never use it as the district key.

### Source label → URL convention

Source labels stored in ChromaDB and returned by the LLM are `{council_file_id}/{filename.pdf}` (e.g. `25-0381/CF-25-0381.pdf`). `rag._source_to_url()` converts these to `https://cityclerk.lacity.org/onlinedocs/{YEAR}/{filename}` where the year is derived from the council file ID prefix.

### LLM output format

The system prompt instructs the model to return JSON with `{answer, sources, followups}`. `sources` is an array of `{title, source}` objects where `title` is a 2–4 word description and `source` is the raw source label. The answer uses inline markdown links `[Short Title](source_label)`. `_parse_llm_output()` handles JSON extraction and falls back to raw text on parse failure. `_rewrite_answer_links()` validates hrefs against a source-label regex and strips malformed ones (model hallucinations in the href) to plain text.

### Key files

- `backend/main.py` — FastAPI app, all endpoints, background seed task
- `backend/rag.py` — retrieval orchestration, caching, source normalisation
- `backend/llm.py` — LLM calls (Claude + OpenAI), system prompt, JSON parsing
- `backend/ingest.py` — chunking + ChromaDB writes
- `backend/downloader.py` — streams ZIPs from scrape-cf, parses PDFs in memory
- `backend/history.py` — SQL engine, chat history, session CRUD, `message_sources` table
- `backend/member_registry.py` — `members` table CRUD (shares engine from history.py)
- `backend/legislation_meta.py` — generates + persists member metadata; `_SEEDS` has hardcoded fallbacks for known districts
- `frontend/src/components/Message.tsx` — renders assistant messages; answer via react-markdown, sources box with `{title, url}` objects

## Conventions to follow

These are load-bearing patterns already established in the code. Match them.

**LLM output is untrusted — normalise server-side, never in the frontend.** The model returns JSON that may be wrapped in code fences, malformed, or hallucinated (e.g. text in a link href). `_parse_llm_output()` strips fences and falls back to raw text; `rag.answer_question()` normalises `sources` and `_rewrite_answer_links()` validates hrefs against `_SOURCE_LABEL`. Any new field coming from the model gets the same treatment: parse defensively, validate, and shape the payload before it leaves the backend. The frontend should render, not repair.

**The source-label format `{council_file_id}/{filename.pdf}` is a contract.** It is written in `ingest.py`, parsed by `_source_to_url()` and `_rewrite_answer_links()`, embedded in the LLM context (`_build_context`), and stored in ChromaDB metadata. Don't change the shape without updating every consumer.

**Keep the two LLM providers in sync.** `_call_claude` and `_call_openai` in `llm.py` are selected by `LLM_PROVIDER`. Any change to the system prompt, expected output structure, or parsing must be applied to both paths (and to `contextualize_question`, which also branches on provider).

**Preserve Anthropic prompt-caching breakpoints.** `_call_claude` marks the system prompt and the document context with `cache_control: ephemeral`. Stable content goes first; the volatile question goes last as a separate, uncached text block. Don't interleave per-request data into cached blocks or you lose the cache hit (and the cost savings).

**Ingest streams everything through memory — never write PDFs or ZIPs to disk.** `downloader.stream_and_parse()` and `ingest_from_memory()` are deliberately diskless (Railway memory/ephemeral-fs constraints). Seeding is throttled (semaphore of 2 downloads, batch size 10) and **idempotent**: already-indexed council files are skipped by checking `council_file` metadata. Preserve idempotency so a re-run after a crash resumes instead of duplicating.

**All SQL DDL goes through `history._ensure_tables()`, and it serves both SQLite and Postgres.** Use `ON CONFLICT ... DO UPDATE/NOTHING` (works on both). When adding a migration, **commit `CREATE TABLE` before any `ALTER`** — Postgres DDL is transactional, so a failed `ALTER` in the same transaction rolls back the table creation too (see the existing `client_id` migration comment). `member_registry.py` imports the engine from `history.py`; don't create a second engine.

**Logging is structured and verbose by design.** Use `logger.get_logger(__name__)` per module. Log each meaningful step at INFO (retrieval counts, token/cache usage, chunk counts) — this is how seeding and RAG behaviour are debugged in production. Background seed tasks get a dedicated per-member file logger: `get_logger(f"seed.{member_id}", log_file=f"seed_{member_id}.log")`.

**When you change a persisted payload shape, handle the old shape on read.** History rows and `legislation_meta.json` outlive deploys. The sources field already carries both legacy `string[]` and current `{title, url}[]` — `Message.tsx` tolerates both. Apply the same care to any stored structure you migrate.

**Member metadata has a code fallback.** `legislation_meta.json` is gitignored runtime data; `_SEEDS` in `legislation_meta.py` is the in-code default (currently only `cd1`). `load_meta()` merges them. A fresh deploy has no JSON until a member is seeded, so don't assume a member's `subtitle`/`starters` exist.

## Scaling & hardening (not yet done — flag before relying on these)

The current design assumes a **single backend process**. These are the known gaps to address as the app grows; treat them as caveats, not existing guarantees.

- **In-memory state breaks horizontal scaling.** `seed_jobs` (main.py), `_answer_cache` / `_collections` / `_session_chunks` (rag.py) all live in process memory. With more than one worker/replica, seed-status polling, the answer cache, and follow-up chunk carry-forward all become inconsistent. Run single-worker for now; move this state to Redis or the DB before scaling out.
- **`seed_jobs` is volatile.** A redeploy or crash mid-seed loses job status (indexing itself resumes via idempotency, but the client loses its progress handle). Persist job state if seed reliability matters.
- **No auth or rate limiting.** `/api/chat` (LLM cost) and `/api/members` (triggers expensive scrape + index) are open. Add authentication and rate limiting before any public exposure.
- **ChromaDB is local-disk.** It can't be shared across replicas and isn't backed up by the app. For scale, move to a hosted/networked vector store; for safety, ensure the volume is backed up.
- **No automated tests.** The pure functions are the cheapest, highest-value place to start: `_source_to_url`, `_rewrite_answer_links`, `_parse_llm_output`, and the council-file-ID regex parsing in `_run_member_seed`. Add these before refactoring any of them.
- **Answer cache ignores the LLM provider.** `_cache_key` keys on question + legislations only. If you switch `LLM_PROVIDER` without clearing the cache, you'll serve answers from the other model. Include the provider in the key if both are used in one environment.
