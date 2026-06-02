from __future__ import annotations

import hashlib
import os
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
) -> dict:
    leg_ids = sorted(legislations)
    log.info("Query for %s: '%s'", leg_ids, question)

    key = _cache_key(question + "|legs:" + ",".join(leg_ids), "__multi__")
    use_cache = not session_id
    if not use_cache and session_id:
        from history import load_recent
        use_cache = len(load_recent(session_id)) == 0

    if use_cache and key in _answer_cache:
        log.info("Cache HIT — returning stored answer")
        return _answer_cache[key]

    log.info("Cache MISS — querying %d collection(s), top %d each", len(leg_ids), TOP_K)

    all_results: list[tuple[float, str, dict]] = []
    for leg in leg_ids:
        try:
            collection = _get_collection(leg)
        except Exception as e:
            log.warning("Could not load collection for '%s': %s", leg, e)
            continue

        res = collection.query(
            query_texts=[question],
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

    result = get_response(question, chunks, leg_ids, session_id=session_id)
    result["sources"] = [_source_to_url(s) for s in result.get("sources", [])]

    if session_id:
        from history import save_exchange
        save_exchange(session_id, question, result.get("answer", ""),
                      sources=result.get("sources", []),
                      followups=result.get("followups", []),
                      member_id=leg_ids[0] if leg_ids else None,
                      client_id=client_id)

    if use_cache:
        _answer_cache[key] = result
        for leg in leg_ids:
            _answer_cache_legs.setdefault(leg, set()).add(key)
        log.info("Answer cached (cache size: %d)", len(_answer_cache))
    return result
