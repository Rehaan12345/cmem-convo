# Technical Brief

This document covers the engineering decisions behind cmem-convo — why each was
made, what it buys, and what it costs. Examples are drawn from the actual code.
For a user-facing tour and the API, see [`OVERVIEW.md`](./OVERVIEW.md).

The guiding principle throughout: **deterministic logic wherever the source
structure permits; LLM calls only where they earn their cost.** The pipeline uses
exactly three model touchpoints — answering, query rewriting, and one-time
metadata generation — and pushes everything else (ID extraction, URL
construction, chunk dedup, source validation) into parsers and regex.

---

## 1. Retrieval-augmented generation, not a fine-tune

**Decision.** Ground answers in retrieved document chunks rather than encoding the
corpus into model weights.

**Why.** The corpus is per-member, changes every time a member acts, and must be
*traceable* — a resident needs to see the exact document a claim came from. Both
properties rule out fine-tuning. RAG keeps the source of truth in a vector store
that can be re-indexed cheaply, and lets every answer carry citations.

**How it shows up.** The system prompt (`llm.py`) is blunt about grounding:

```
1. Base your answer ONLY on the document excerpts provided. Do not use outside knowledge.
...
4. If the documents don't have enough to fully answer the question:
   - Say clearly: "I don't know based on these documents."
```

**Trade-off.** Answer quality is bounded by retrieval quality. If the top-K chunks
miss the relevant document, the model correctly says it doesn't know — which is
safer than hallucinating, but means retrieval tuning (chunk size, K, the query
rewrite) is where the real quality lives.

## 2. Local embeddings (`all-MiniLM-L6-v2`), not an embedding API

**Decision.** Embed with a local sentence-transformers model instead of a hosted
embedding endpoint.

**Why.** Indexing a single member can mean hundreds of council files and thousands
of chunks. A local model makes embedding cost zero-marginal and removes a network
dependency and rate limit from the hottest loop in seeding. The model is ~80 MB and
pre-baked into the Docker image so containers start without a download:

```dockerfile
# backend/Dockerfile
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
```

**Trade-off.** `all-MiniLM-L6-v2` is a small, general-purpose model — weaker than
the best hosted embeddings on nuanced semantic match, and not domain-tuned for
municipal-legal text. For this product the cost/latency win outweighs the marginal
recall gain, but it's the first knob to revisit if retrieval quality plateaus.

## 3. ChromaDB on local disk, one collection per member

**Decision.** Persist embeddings in ChromaDB at `DB_PATH`, isolated per member in a
collection named `leg_{member_id}`.

**Why.** Per-member collections make the multi-jurisdiction case clean: adding,
re-indexing, or deleting one member never touches another's data, and a query only
ever scans one member's corpus. `ingest.collection_name()` is the single source of
that naming:

```python
def collection_name(legislation_id: str) -> str:
    return f"leg_{legislation_id.replace('-', '_')}"
```

**Trade-off (flagged, not solved).** Local disk can't be shared across replicas and
isn't backed up by the app. This is the main thing pinning the system to a single
process. A hosted/networked vector store is the migration path before horizontal
scale; until then, the volume must be backed up out-of-band.

## 4. Diskless, idempotent seeding

**Decision.** Stream council-file ZIPs, extract and parse PDFs entirely in memory,
and never write source files to disk. Make re-runs resume instead of duplicate.

**Why.** Railway gives ephemeral filesystem and bounded memory — writing hundreds of
PDFs to disk is both wasteful and fragile. `downloader.stream_and_parse()` reads the
ZIP into a `BytesIO`, parses each PDF from memory, and returns `(filename, text)`
pairs; nothing hits disk.

Idempotency is what makes seeding survivable. Before downloading a file, the worker
checks whether it's already in the collection:

```python
# main.py — _run_indexing / process_one
existing = collection.get(where={"council_file": file_id}, limit=1, include=[])
if existing["ids"]:
    seed_log.info("Skipping %s — already indexed", file_id)
    completed += 1
    return
```

So a crash mid-seed resumes from where it stopped on the next run, rather than
double-indexing everything before it.

**Concurrency is deliberately throttled** — a semaphore of 2 concurrent downloads,
processed in batches of 10 — to stay within memory and be polite to the upstream
scraper:

```python
sem = asyncio.Semaphore(2)
BATCH_SIZE = 10
for i in range(0, len(file_ids), BATCH_SIZE):
    await asyncio.gather(*[process_one(fid) for fid in file_ids[i:i + BATCH_SIZE]])
```

**Trade-off.** Seeding is slow by design (it's not the user-facing hot path, so this
is the right call), and **`seed_jobs` is in-memory** — a redeploy mid-seed loses the
client's progress handle. Indexing itself resumes via idempotency; only the status
polling breaks. Persisting job state is deferred until seed reliability matters.

## 5. Deterministic source-label → URL contract

**Decision.** Encode each chunk's provenance as a string label
`{council_file_id}/{filename.pdf}` and reconstruct the public URL from it
arithmetically — no lookup table, no model involvement.

**Why.** The council-file ID already contains the year (`25-0381` → 2025), and
`cityclerk.lacity.org` URLs are structured. So the label is all the information
needed to build a real link deterministically:

```python
def _source_to_url(source: str) -> str:
    cf_id, filename = source.split("/", 1)
    year = 2000 + int(cf_id.split("-")[0])
    return f"https://cityclerk.lacity.org/onlinedocs/{year}/{filename}"
```

This label is a **contract** written in `ingest.py`, embedded into the LLM context,
stored in ChromaDB metadata, and parsed on the way out. Every consumer depends on
its shape — changing it means updating all of them.

**Trade-off.** It bakes one jurisdiction's URL convention into a core function. The
honest assessment: this is the spot where LA-specific logic has leaked into the core
pipeline. Multi-city replication would need `_source_to_url` to become
jurisdiction-aware (e.g. a per-city resolver) rather than hardcoding the
`onlinedocs/{year}/` pattern.

## 6. LLM output is untrusted — parse and validate server-side

**Decision.** Treat every field the model returns as hostile input: parse
defensively, validate against known patterns, and shape the payload before it
leaves the backend. The frontend renders; it never repairs.

**Why.** The model is asked to return JSON, but it may wrap it in code fences,
malformed it, or hallucinate content into a link href. Three layers handle this:

**(a) Fence-tolerant JSON parse with a raw-text fallback** (`_parse_llm_output`):

```python
raw = re.sub(r"^```(?:json)?\s*", "", raw)
raw = re.sub(r"\s*```$", "", raw)
try:
    return json.loads(raw)
except json.JSONDecodeError as e:
    log.warning("Failed to parse LLM output as JSON (%s); falling back to raw text", e)
    return {"answer": raw, "sources": [], "followups": []}
```

**(b) Href validation against the source-label regex** (`_rewrite_answer_links`).
The model is told to put *only* a source label in each href, but it sometimes adds
dollar amounts or stray words. Hrefs that are real labels get rewritten to URLs;
malformed ones are demoted to plain text so nothing broken reaches the UI:

```python
def repl(m):
    text, href = m.group(1), m.group(2)
    if href in label_to_url:
        return f"[{text}]({label_to_url[href]})"
    if _SOURCE_LABEL.match(href):
        return f"[{text}]({_source_to_url(href)})"
    return text  # malformed href — keep the visible text, drop the bad link
```

**(c) Defensive normalisation of `topic_starters`** in `legislation_meta.py` — only
`{str: [str, …]}` entries survive, capped in count and length.

**Trade-off.** More backend code and a few defensive branches that "shouldn't" be
needed if the model behaved. The cost is cheap and the alternative — broken links or
a crashed render in a civic tool residents are meant to trust — is not.

## 7. History-aware retrieval via query contextualization

**Decision.** Before retrieving for a follow-up, rewrite the user's message into a
standalone search query using the conversation so far — a second, cheap LLM call.

**Why.** Follow-ups like "say more" or "what documents is that from?" carry no topic
on their own. Embedding them directly retrieves irrelevant chunks. Rewriting
restores the topic:

```python
# rag.answer_question
search_query = question
if prior_messages:
    search_query = contextualize_question(question, prior_messages)
```

`contextualize_question` returns the original question on any error, so retrieval
always proceeds even if the rewrite call fails.

**Trade-off.** A deliberate extra LLM call per follow-up turn — a justified cost,
since the alternative (retrieving on a contentless query) defeats the whole point of
a follow-up. It only fires when there's history, and it uses the cheap Haiku/4o-mini
model with a 128-token cap.

## 8. Chunk carry-forward across turns

**Decision.** Keep the previous turn's retrieved chunks in memory per session and
prepend them (deduplicated) to the next turn's retrieval.

**Why.** A follow-up about the prior answer needs that answer's grounding even if the
new query retrieves a slightly different set. Carry-forward preserves continuity
without re-deriving it:

```python
if session_id:
    carried = _session_chunks.get(session_id, [])
    chunks = _dedup_chunks(chunks + carried)[: TOP_K * len(leg_ids) + TOP_K]
    _session_chunks[session_id] = chunks
```

The slice bounds the context so it can't grow unboundedly across a long conversation.

**Trade-off.** `_session_chunks` is process memory — it breaks across replicas, and a
restart drops mid-conversation grounding (the next turn just re-retrieves). Fine for
single-process; a Redis-backed store is the scale-out path.

## 9. Anthropic prompt caching to control cost

**Decision.** Structure the Claude request so the stable parts (system prompt,
document context) are cached and only the volatile question is uncached.

**Why.** The system prompt and retrieved context are large and repeat across a
session; the question is small and changes every turn. Marking the stable blocks
`cache_control: ephemeral` and putting the question last as a separate, uncached
block means repeated turns pay for cache *reads* (cheap) instead of re-processing
the whole context:

```python
messages.append({
    "role": "user",
    "content": [
        {"type": "text", "text": f"Document excerpts:\n\n{context}",
         "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": f"\n\nQuestion: {question}"},   # volatile, uncached
    ],
})
```

Cache hit/write token counts are logged every call, so the savings are observable in
production logs.

**Trade-off.** The ordering is load-bearing and easy to break — interleaving
per-request data into a cached block silently loses the hit. It's documented as a
convention precisely because it's fragile.

## 10. Two interchangeable LLM providers

**Decision.** Support Claude (default) and OpenAI behind one `LLM_PROVIDER` switch,
with parallel `_call_claude` / `_call_openai` paths.

**Why.** Provider flexibility (cost, availability, evaluation) without rearchitecting.

**Trade-off.** Every prompt/parsing change must be applied to *both* paths plus
`contextualize_question`, or they drift. There's a subtler one: the answer cache key
originally ignored the provider, so switching providers could serve answers from the
wrong model. That's now folded into the key:

```python
provider = os.getenv("LLM_PROVIDER", "claude").lower()
key = _cache_key(question + "|legs:" + ",".join(leg_ids) + "|p:" + provider, "__multi__")
```

Note the two paths aren't fully symmetric: Claude gets native prompt caching and
structured `messages` history; OpenAI gets history flattened into a text preamble
(`_format_history_for_openai`). Same output contract, different cost profile.

## 11. In-memory answer cache, scoped to first-turn questions

**Decision.** Cache answers keyed on `question + members + provider`, but **only**
for questions with no conversation history.

**Why.** First-turn questions (especially starter questions) repeat across users and
are safe to serve identically. Follow-ups depend on a specific conversation and must
not be cached:

```python
use_cache = not prior_messages
if use_cache and key in _answer_cache:
    return _answer_cache[key]
```

The cache is also indexed by member (`_answer_cache_legs`) so re-seeding a member can
invalidate exactly its cached answers, not the whole cache:

```python
def invalidate_collection_cache(legislation):
    _collections.pop(legislation, None)
    for key in _answer_cache_legs.pop(legislation, set()):
        _answer_cache.pop(key, None)
```

**Trade-off.** Process-local, so it's per-replica and lost on restart. Acceptable for
a warm-start optimisation; it would move to Redis alongside the other in-memory
state before scaling out.

## 12. One SQL layer for SQLite and Postgres

**Decision.** Run the same DDL and queries against SQLite (local) and Postgres
(Railway), switching only on whether `DATABASE_URL` is set.

**Why.** Zero-setup local development (SQLite file, WAL mode for concurrency) with a
production-grade database in deployment, without a second code path. All DDL goes
through `history._ensure_tables()` using `ON CONFLICT … DO UPDATE/NOTHING`, which
both engines accept.

The one place the two databases genuinely differ — transactional DDL — is handled
explicitly. Postgres rolls back a whole transaction on a failed `ALTER`, which would
also discard the `CREATE TABLE`s, so creation is committed before any migration:

```python
conn.commit()  # commit CREATE TABLEs before the ALTER migrations below
try:
    conn.execute(text("ALTER TABLE sessions ADD COLUMN client_id TEXT"))
    conn.commit()
except Exception:
    conn.rollback()  # column already exists
```

**Trade-off.** "Add a column in a try/except" is a homegrown migration strategy, fine
at this size but not a substitute for real migrations (Alembic) as the schema grows.

## 13. Rate limiting by real client IP

**Decision.** Apply per-IP limits with `slowapi`, tighter on the expensive routes.

**Why.** `/api/chat` costs LLM tokens and `/api/members` triggers an expensive
scrape+index, and both are unauthenticated. Limits cap abuse: 20/min + 300/day on
chat, 3/hour on seeding/reseeding, 120/min default elsewhere, health exempt.

Behind Railway's single trusted proxy, the real client is the first
`X-Forwarded-For` entry:

```python
def client_ip(request):
    xff = request.headers.get("x-forwarded-for")
    return xff.split(",")[0].strip() if xff else (request.client.host if request.client else "unknown")
```

**Trade-off.** IP-based limiting is the right *first* layer but not a substitute for
auth — there is none yet. The limits assume exactly one trusted proxy; the
`X-Forwarded-For` parsing would need revisiting behind a different topology.

---

## Known gaps (be honest about these)

These are real limitations, not hidden ones. Treat them as caveats before relying on
behaviour the current design doesn't actually guarantee:

- **Single-process assumption.** `seed_jobs`, `_answer_cache`, `_collections`, and
  `_session_chunks` all live in process memory. With more than one worker, seed
  status, the answer cache, and follow-up carry-forward go inconsistent. Run one
  worker until this state moves to Redis/DB.
- **ChromaDB is local-disk** — not shareable across replicas, not backed up by the
  app.
- **No auth.** Rate limiting is the only gate on cost-bearing endpoints.
- **No automated tests.** The cheapest, highest-value place to start is the pure
  functions: `_source_to_url`, `_rewrite_answer_links`, `_parse_llm_output`, and the
  council-file-ID regex in `_run_member_seed`. Add these before refactoring any of
  them — they're the load-bearing parsers and the contract enforcers.
- **One jurisdiction in the core.** `_source_to_url` hardcodes LA's URL convention.
  Multi-city replication requires isolating that behind a jurisdiction-aware
  resolver so the core pipeline stays untouched per city added.

## What would falsify the design choices

A few of these decisions are bets worth stating a test for:

- **Local embeddings are good enough.** Falsified if a retrieval eval set shows
  `all-MiniLM-L6-v2` materially missing relevant chunks that a hosted embedding
  model catches. There is no eval set today — that's the gap to close before
  claiming "research-grounded" retrieval.
- **Starter questions drive engagement.** `GET /api/stats/starters` exists to
  measure this: the starter conversion rate and per-topic breakdown. If the rate is
  low, the starter feature is mostly serving power users and the blank-page problem
  is unsolved.
- **Caching first-turn answers is safe.** Falsified if identical first-turn
  questions should legitimately produce different answers (e.g. after a re-seed that
  isn't invalidated) — which is why invalidation is keyed per member.
