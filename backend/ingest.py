"""
Indexing utilities for the cmem-convo vector database.

ingest_from_memory() is the primary entry point — it takes (council_file_id, filename, text)
tuples already parsed from PDFs in memory and adds them to a ChromaDB collection.

ingest_legislation() is retained for startup auto-ingest of any on-disk folders.
"""
import os
import sys
from pathlib import Path

import pdfplumber
import chromadb
from chromadb.utils import embedding_functions
from dotenv import load_dotenv

from logger import get_logger

load_dotenv()

log = get_logger(__name__)

DB_PATH = Path(os.getenv("DB_PATH", str(Path(__file__).parent / "chroma_db")))
CHUNK_SIZE = 400
OVERLAP = 50


def collection_name(legislation_id: str) -> str:
    return f"leg_{legislation_id.replace('-', '_')}"


def chunk_text(text: str, source: str) -> list[dict]:
    words = text.split()
    chunks = []
    start = 0
    while start < len(words):
        end = min(start + CHUNK_SIZE, len(words))
        chunks.append({"text": " ".join(words[start:end]), "source": source})
        if end == len(words):
            break
        start += CHUNK_SIZE - OVERLAP
    return chunks


def ingest_from_memory(
    member_id: str,
    pdf_tuples: list[tuple[str, str, str]],  # (council_file_id, filename, text)
    client: chromadb.PersistentClient,
    ef: embedding_functions.SentenceTransformerEmbeddingFunction,
    collection=None,
) -> int:
    """
    Add chunks from in-memory parsed PDFs to the member's ChromaDB collection.
    Returns number of chunks added.
    """
    if collection is None:
        coll_name = collection_name(member_id)
        collection = client.get_collection(coll_name, embedding_function=ef)

    all_ids, all_texts, all_metas = [], [], []

    for council_file_id, filename, text in pdf_tuples:
        if not text.strip():
            log.warning("No text in %s/%s — skipping", council_file_id, filename)
            continue
        source_label = f"{council_file_id}/{filename}"
        for i, chunk in enumerate(chunk_text(text, source_label)):
            all_ids.append(f"{council_file_id}__{Path(filename).stem}_{i}")
            all_texts.append(chunk["text"])
            all_metas.append({
                "source": source_label,
                "council_file": council_file_id,
                "filename": filename,
                "member": member_id,
            })

    if not all_ids:
        return 0

    for i in range(0, len(all_ids), 500):
        collection.add(
            ids=all_ids[i:i + 500],
            documents=all_texts[i:i + 500],
            metadatas=all_metas[i:i + 500],
        )

    log.info("Added %d chunks for %d PDFs into member=%s", len(all_ids), len(pdf_tuples), member_id)
    return len(all_ids)


def ingest_legislation(legislation_folder: Path, client: chromadb.PersistentClient,
                       ef: embedding_functions.SentenceTransformerEmbeddingFunction) -> int:
    """Legacy on-disk ingest — used only by startup auto-ingest of existing folders."""
    legislation_id = legislation_folder.name
    coll_name = collection_name(legislation_id)

    pdf_files = sorted(legislation_folder.rglob("*.pdf"))
    if not pdf_files:
        log.warning("No PDFs found in %s", legislation_folder)
        return 0

    log.info("[%s] %d PDFs → collection '%s'", legislation_id, len(pdf_files), coll_name)

    try:
        client.delete_collection(coll_name)
    except Exception:
        pass
    collection = client.create_collection(coll_name, embedding_function=ef)

    all_ids, all_texts, all_metadatas = [], [], []

    for pdf_path in pdf_files:
        subfolder = pdf_path.parent.name
        source_label = f"{subfolder}/{pdf_path.name}"
        log.info("  Parsing %s...", source_label)
        try:
            with pdfplumber.open(pdf_path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages).strip()
        except Exception as e:
            log.warning("  Could not parse %s: %s", source_label, e)
            continue

        if not text:
            log.warning("  No text extracted from %s", source_label)
            continue

        for i, chunk in enumerate(chunk_text(text, source_label)):
            all_ids.append(f"{subfolder}__{pdf_path.stem}_{i}")
            all_texts.append(chunk["text"])
            all_metadatas.append({
                "source": source_label,
                "subfolder": subfolder,
                "filename": pdf_path.name,
                "legislation": legislation_id,
            })

    for i in range(0, len(all_ids), 500):
        collection.add(
            ids=all_ids[i:i + 500],
            documents=all_texts[i:i + 500],
            metadatas=all_metadatas[i:i + 500],
        )

    log.info("Indexed %d chunks from %d files for %s", len(all_ids), len(pdf_files), legislation_id)
    return len(all_ids)
