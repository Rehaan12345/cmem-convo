"""
Generates and persists human-readable metadata for each indexed member corpus.

Metadata is stored in legislation_meta.json and survives server restarts.
On first seed of a new member, generate_and_save_meta() is called automatically.
"""
import json
import os
import re
import threading
import time
from pathlib import Path

import anthropic
from chromadb.utils import embedding_functions

from logger import get_logger

log = get_logger(__name__)

_meta_lock = threading.Lock()

META_PATH = Path(os.getenv("META_PATH", str(Path(__file__).parent / "legislation_meta.json")))
DB_PATH   = Path(os.getenv("DB_PATH",   str(Path(__file__).parent / "chroma_db")))

_SEEDS: dict[str, dict] = {
    "cd1": {
        "subtitle": "Councilmember Eunisses Hernandez — District 1",
        "context": (
            "the legislative record of Councilmember Eunisses Hernandez, Council District 1, "
            "City of Los Angeles. These 307 council files span housing policy, homelessness response, "
            "public safety, immigration rights, infrastructure, environmental justice, and community "
            "services across CD1 neighborhoods including MacArthur Park, Chinatown, Highland Park, "
            "and Westlake."
        ),
        "starters": [
            "What has the councilmember done about homelessness in CD1?",
            "What is her stance on immigration enforcement and sanctuary policies?",
            "What housing legislation has she supported or introduced?",
            "What community programs has she funded in District 1?",
        ],
    },
}


def load_meta() -> dict:
    data: dict = dict(_SEEDS)
    if META_PATH.exists():
        try:
            with open(META_PATH) as f:
                stored = json.load(f)
            data.update(stored)
        except Exception as e:
            log.warning("Could not read %s: %s", META_PATH, e)
    return data


def save_meta(meta: dict):
    with open(META_PATH, "w") as f:
        json.dump(meta, f, indent=2)
    log.info("Saved %s (%d entries)", META_PATH, len(meta))


def get_meta(member_id: str) -> dict | None:
    return load_meta().get(member_id)


def generate_and_save_meta(member_id: str) -> dict:
    from ingest import collection_name
    log.info("Generating metadata for '%s'...", member_id)

    ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    from rag import get_chroma_client
    client = get_chroma_client()
    try:
        collection = client.get_collection(collection_name(member_id), embedding_function=ef)
    except Exception as e:
        log.error("Could not load collection for '%s': %s", member_id, e)
        return {}

    results = collection.query(
        query_texts=["What does this councilmember work on? What policies and legislation?"],
        n_results=5,
        include=["documents", "metadatas"],
    )

    chunks = []
    if results["documents"] and results["documents"][0]:
        for doc, m in zip(results["documents"][0], results["metadatas"][0]):
            chunks.append(f"[Source: {m.get('source', 'unknown')}]\n{doc}")
    context_text = "\n\n---\n\n".join(chunks)

    prompt = f"""You are helping build a public-facing app that lets LA residents ask questions about their city council representative.

Below are document excerpts from the council file corpus for member ID "{member_id}". Based only on these excerpts, return ONLY a JSON object with this exact shape (no markdown, no extra text):

{{
  "subtitle": "A short description under 10 words — like 'Councilmember Jane Smith — District 5'",
  "context": "2-3 sentences describing what this councilmember's corpus covers. Written for an AI assistant that will answer resident questions. Include the councilmember's name if you can infer it.",
  "starters": [
    "What has this councilmember done about housing?",
    "A specific question a resident could ask (answerable from these documents)",
    "Another specific question a resident could ask",
    "A fourth specific question a resident could ask"
  ],
  "topic_starters": {{
    "Topic Label": [
      "A specific question on this topic a resident could ask (answerable from these documents)",
      "Another specific question on this topic"
    ]
  }}
}}

For "topic_starters", pick 3-5 short topic labels (1-3 words each, Title Case) that reflect the main themes actually present in these documents, and give 2-3 specific resident questions per topic. Topics must come from the excerpts — do not invent themes not supported by the documents.

Document excerpts:
{context_text}"""

    from llm import _get_anthropic_client
    anthropic_client = _get_anthropic_client()

    raw = None
    for attempt in range(4):
        try:
            response = anthropic_client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=1200,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.content[0].text.strip()
            break
        except anthropic.RateLimitError:
            wait = 60 * (attempt + 1)
            log.warning("Rate limit for '%s' — waiting %ds (attempt %d/4)", member_id, wait, attempt + 1)
            time.sleep(wait)
        except Exception as e:
            log.error("API error for '%s': %s", member_id, e)
            break

    if raw is None:
        log.warning("All retries exhausted for '%s' — using fallback", member_id)
        raw = ""

    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        generated = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Could not parse response for '%s' — using fallback", member_id)
        generated = {
            "subtitle": f"Council Member {member_id}",
            "context": f"Legislative record for council member {member_id}.",
            "starters": [
                "What has this councilmember worked on?",
                "What housing legislation have they supported?",
                "What community programs have they funded?",
                "What is their stance on public safety?",
            ],
        }

    starters = generated.get("starters", [])
    generated["starters"] = starters[:4]

    # Normalize topic_starters defensively — model output is untrusted. Keep only
    # {topic: [question, ...]} entries with a non-empty list of string questions.
    raw_topics = generated.get("topic_starters")
    topic_starters: dict[str, list[str]] = {}
    if isinstance(raw_topics, dict):
        for label, qs in raw_topics.items():
            if not isinstance(label, str) or not isinstance(qs, list):
                continue
            clean = [q for q in qs if isinstance(q, str) and q.strip()][:3]
            if clean:
                topic_starters[label.strip()] = clean
    generated["topic_starters"] = dict(list(topic_starters.items())[:5])

    with _meta_lock:
        meta = load_meta()
        meta[member_id] = generated
        save_meta(meta)

    log.info("'%s' → %s", member_id, generated.get("subtitle"))
    return generated


def ensure_meta(member_id: str) -> dict:
    existing = get_meta(member_id)
    if existing:
        return existing
    return generate_and_save_meta(member_id)
