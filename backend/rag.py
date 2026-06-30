from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

from llm import get_response
from ingest import collection_name
from logger import get_logger

log = get_logger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).parent / "chroma_db")))
TOP_K = 5

_collections: dict = {}
_answer_cache: dict[str, dict] = {}
_answer_cache_legs: dict[str, set] = {}
# Most recent retrieved chunks per session, carried into follow-up turns so a
# question about the prior answer keeps that answer's grounding in context.
_session_chunks: dict[str, list[dict]] = {}

_chroma_client: chromadb.PersistentClient | None = None
_chroma_lock = threading.Lock()
_collections_lock = threading.Lock()


def _source_to_url(source: str) -> str:
    """Convert 'CF-ID/filename.pdf' → cityclerk.lacity.org direct PDF URL."""
    parts = source.split("/", 1)
    if len(parts) != 2:
        return source
    cf_id, filename = parts
    year_prefix = cf_id.split("-")[0]
    try:
        year = 2000 + int(year_prefix)
    except ValueError:
        return source
    return f"https://cityclerk.lacity.org/onlinedocs/{year}/{filename}"


_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_SOURCE_LABEL = re.compile(r"^\d{2}-\d{4}/\S+\.pdf$")


def _rewrite_answer_links(answer: str, label_to_url: dict[str, str]) -> str:
    """Rewrite markdown link hrefs from source labels to real PDF URLs.

    Hrefs in label_to_url are replaced directly. Hrefs that look like source
    labels but aren't in the map fall back to _source_to_url. Hrefs that don't
    match the source-label pattern at all (malformed output from the model) are
    stripped — the link becomes plain text so nothing broken reaches the UI."""
    def repl(m: re.Match) -> str:
        text, href = m.group(1), m.group(2)
        if href in label_to_url:
            return f"[{text}]({label_to_url[href]})"
        if _SOURCE_LABEL.match(href):
            return f"[{text}]({_source_to_url(href)})"
        return text  # malformed href — keep the visible text, drop the bad link
    return _MD_LINK.sub(repl, answer)


def _dedup_chunks(chunks: list[dict]) -> list[dict]:
    """Drop duplicate chunks (same source + text), preserving order."""
    seen = set()
    out = []
    for c in chunks:
        k = (c["source"], c["text"])
        if k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _aggregation_chunks(rows: list[dict]) -> list[dict]:
    """Turn structured aggregation rows (from citywise.get_member_legislation)
    into pseudo-chunks so the existing citation/fact-card pipeline handles them
    unchanged. Rows without a real document URL are dropped (no citable source)."""
    chunks = []
    for r in rows:
        label = r.get("source_label")
        if not label:
            continue
        date = r["start_date"].strftime("%Y-%m-%d") if r.get("start_date") else "n/a"
        text = (f"Council file {r['cf_id']}: {r.get('name') or ''} "
                f"(status: {r.get('status') or 'n/a'}, date: {date}).")
        chunks.append({"text": text, "source": label})
    return chunks


def _cache_key(question: str, legislation: str) -> str:
    normalized = question.lower().strip()
    return hashlib.sha256(f"{legislation}:{normalized}".encode()).hexdigest()


def get_chroma_client() -> chromadb.PersistentClient:
    global _chroma_client
    if _chroma_client is None:
        with _chroma_lock:
            if _chroma_client is None:
                _chroma_client = chromadb.PersistentClient(path=str(DB_PATH))
    return _chroma_client


def _get_collection(legislation: str):
    if legislation in _collections:
        return _collections[legislation]
    with _collections_lock:
        if legislation not in _collections:
            coll_name = collection_name(legislation)
            log.info("Loading ChromaDB collection '%s'", coll_name)
            ef = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name="all-MiniLM-L6-v2"
            )
            _collections[legislation] = get_chroma_client().get_collection(
                coll_name, embedding_function=ef
            )
            log.info("Loaded '%s' (%d chunks)", coll_name, _collections[legislation].count())
    return _collections[legislation]


def get_chunk_count(legislation: str) -> int:
    try:
        return _get_collection(legislation).count()
    except Exception as e:
        log.error("Error getting chunk count for '%s': %s", legislation, e)
        return 0


def get_available_legislations() -> list[dict]:
    try:
        client = get_chroma_client()
        result = []
        for col in client.list_collections():
            if col.name.startswith("leg_"):
                leg_id = col.name.removeprefix("leg_").replace("_", "-")
                result.append({"id": leg_id, "chunks": col.count()})
        return sorted(result, key=lambda x: x["id"])
    except Exception as e:
        log.error("Error listing legislations: %s", e)
        return []


def invalidate_collection_cache(legislation: str):
    with _collections_lock:
        _collections.pop(legislation, None)
    for key in _answer_cache_legs.pop(legislation, set()):
        _answer_cache.pop(key, None)
    log.info("Invalidated collection cache for '%s'", legislation)


def delete_member_collection(member_id: str) -> int:
    coll_name = collection_name(member_id)
    client = get_chroma_client()
    try:
        col = client.get_collection(coll_name)
        chunk_count = col.count()
    except Exception:
        chunk_count = 0
    try:
        client.delete_collection(coll_name)
        log.info("Deleted ChromaDB collection '%s' (%d chunks)", coll_name, chunk_count)
    except Exception as e:
        log.warning("Could not delete collection '%s': %s", coll_name, e)
    invalidate_collection_cache(member_id)
    return chunk_count


def answer_question(
    question: str,
    legislations: list[str],
    session_id: str | None = None,
    client_id: str | None = None,
    from_starter: bool = False,
    starter_topic: str | None = None,
) -> dict:
    leg_ids = sorted(legislations)
    log.info("Query for %s: '%s'", leg_ids, question)

    prior_messages = []
    if session_id:
        from history import load_recent
        prior_messages = load_recent(session_id)

    provider = os.getenv("LLM_PROVIDER", "claude").lower()
    key = _cache_key(question + "|legs:" + ",".join(leg_ids) + "|p:" + provider, "__multi__")
    use_cache = not prior_messages

    if use_cache and key in _answer_cache:
        log.info("Cache HIT — returning stored answer")
        return _answer_cache[key]

    # Resolve the app member to a name + district up front so both the
    # aggregation router and fact-card grounding can use it.
    member_name, district = None, None
    if leg_ids:
        from member_registry import get_member
        member = get_member(leg_ids[0])
        if member:
            member_name = member.get("name")
            dm = re.search(r"\d+", member.get("district") or "")
            district = int(dm.group()) if dm else None

    # Structured intent routing: vote-record and sponsorship questions are
    # whole-record queries vector search cannot answer (and for which the seeded
    # collection is a tiny, skewed subset). A cheap LLM classifier decides; on a
    # structured intent we answer from SQL (the council files become the context)
    # and skip retrieval. Self-contained, so it runs regardless of history, and
    # before the follow-up rewrite so it doesn't burn a contextualize call.
    chunks: list[dict] = []
    vote_summary: str | None = None
    if member_name:
        from llm import classify_question_intent
        cls = classify_question_intent(question)
        intent = cls["intent"]
        if intent == "vote_record":
            from citywise import get_member_votes, render_vote_summary
            record = get_member_votes(member_name, district, cls["vote_values"])
            chunks = _aggregation_chunks(record["rows"])
            if record["counts"]:
                vote_summary = render_vote_summary(
                    member_name, record["counts"], cls["vote_values"], len(chunks))
                log.info("Vote-record intent -> %d sample files (skipping vector search)",
                         len(chunks))
        elif intent == "sponsored":
            from citywise import get_member_legislation
            chunks = _aggregation_chunks(get_member_legislation(member_name, district))
            if chunks:
                log.info("Sponsored intent -> %d structured files (skipping vector search)",
                         len(chunks))
        elif intent == "nc_district":
            # Which neighborhood councils are in / have filed CIS in this district.
            # The active member fixes the district; answer from the geographic NC
            # mapping (summary-only — no per-NC document to cite). Skip retrieval.
            from citywise import get_ncs_in_district, render_nc_summary
            nc_rows = get_ncs_in_district(district)
            if nc_rows:
                vote_summary = render_nc_summary(district, nc_rows)
                log.info("NC-district intent -> %d NCs in CD%s (skipping vector search)",
                         len(nc_rows), district)

    if not chunks and not vote_summary:
        # On a follow-up, rewrite the question into a standalone query so retrieval
        # stays on-topic ("what documents is this from?" -> the prior answer's topic).
        search_query = question
        if prior_messages:
            from llm import contextualize_question
            search_query = contextualize_question(question, prior_messages)

        log.info("Cache MISS — querying %d collection(s), top %d each", len(leg_ids), TOP_K)
        all_results: list[tuple[float, str, dict]] = []
        for leg in leg_ids:
            try:
                collection = _get_collection(leg)
            except Exception as e:
                log.warning("Could not load collection for '%s': %s", leg, e)
                continue

            res = collection.query(
                query_texts=[search_query],
                n_results=TOP_K,
                include=["documents", "metadatas", "distances"],
            )
            if res["documents"] and res["documents"][0]:
                for doc, meta, dist in zip(
                    res["documents"][0], res["metadatas"][0], res["distances"][0]
                ):
                    all_results.append((dist, doc, meta))

        all_results.sort(key=lambda x: x[0])
        top = all_results[: TOP_K * len(leg_ids)]

        chunks = [{"text": doc, "source": meta.get("source", "unknown")}
                  for _, doc, meta in top]

        log.info("Retrieved %d chunks total", len(chunks))
        for i, c in enumerate(chunks):
            log.info("  [%d] %s — %d words", i + 1, c["source"], len(c["text"].split()))

        # Carry the prior turn's chunks forward so follow-ups keep their grounding,
        # then store this turn's context (bounded) for the next follow-up.
        if session_id:
            carried = _session_chunks.get(session_id, [])
            chunks = _dedup_chunks(chunks + carried)[: TOP_K * len(leg_ids) + TOP_K]
            _session_chunks[session_id] = chunks
            log.info("Context after carry-forward: %d chunks", len(chunks))

    # Structured grounding: pull verified facts (type/status/sponsors/votes) for
    # the council files now in context so the model states them from the DB
    # instead of guessing from PDF prose. Degrades to RAG-only if unavailable.
    cf_ids: list[str] = []
    for c in chunks:
        cf = c["source"].split("/", 1)[0]
        if cf and cf not in cf_ids:
            cf_ids.append(cf)

    from citywise import get_fact_cards
    fact_cards = get_fact_cards(cf_ids, member_name=member_name, district=district)

    result = get_response(question, chunks, leg_ids, session_id=session_id,
                          fact_cards=fact_cards, vote_summary=vote_summary)

    # Normalize sources to {title, url} objects and rewrite the answer's inline
    # link hrefs (source labels) to real URLs. Tolerate bare-string sources from
    # legacy output or the JSON parse fallback.
    label_to_url: dict[str, str] = {}
    normalized: list[dict] = []
    for s in result.get("sources", []):
        if isinstance(s, dict):
            label = s.get("source", "")
            title = s.get("title") or label.split("/")[-1]
        else:
            label = s
            title = label.split("/")[-1]
        url = _source_to_url(label)
        label_to_url[label] = url
        normalized.append({"title": title, "url": url})
    result["sources"] = normalized
    result["answer"] = _rewrite_answer_links(result.get("answer", ""), label_to_url)

    if session_id:
        from history import save_exchange
        save_exchange(session_id, question, result.get("answer", ""),
                      sources=result.get("sources", []),
                      followups=result.get("followups", []),
                      member_id=leg_ids[0] if leg_ids else None,
                      client_id=client_id,
                      from_starter=from_starter,
                      starter_topic=starter_topic)

    if use_cache:
        _answer_cache[key] = result
        for leg in leg_ids:
            _answer_cache_legs.setdefault(leg, set()).add(key)
        log.info("Answer cached (cache size: %d)", len(_answer_cache))
    return result
