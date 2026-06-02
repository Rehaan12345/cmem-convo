import asyncio
import io
import os
import re
import uuid

import pdfplumber
from fastapi import FastAPI, HTTPException, BackgroundTasks, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from chromadb.utils import embedding_functions

import rag
import member_registry
import legislation_meta as leg_meta
from ingest import collection_name, ingest_from_memory
from logger import get_logger
import downloader

load_dotenv()

log = get_logger(__name__)

# ── Council file ID pattern found in district PDFs ────────────────────────────
_CF_RE = re.compile(r"\b(\d{2}-\d{4}(?:-S\d+)?)\b")


app = FastAPI(title="Council Member Chat API")

_raw_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)

# In-memory job tracker for background seed tasks
seed_jobs: dict[str, dict] = {}


# ── Pydantic Models ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    question: str
    member_id: str
    session_id: str | None = None
    client_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    sources: list[str]
    followups: list[str]


class MemberSeedStarted(BaseModel):
    job_id: str
    member_id: str


class MemberSeedStatus(BaseModel):
    job_id: str
    member_id: str
    status: str    # "parsing" | "indexing" | "done" | "error"
    message: str


# ── Background seed task ──────────────────────────────────────────────────────

async def _run_indexing(
    job_id: str,
    member_id: str,
    files_list: list[dict],
    seed_log,
) -> None:
    """Wipe + recreate ChromaDB collection, download + index all files, generate metadata."""
    job = seed_jobs[job_id]
    file_ids = [f["id"] for f in files_list]
    total = len(file_ids)

    ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
    client = rag.get_chroma_client()
    coll_name = collection_name(member_id)
    try:
        client.delete_collection(coll_name)
        seed_log.info("Wiped existing collection '%s'", coll_name)
    except Exception:
        pass
    collection = client.create_collection(coll_name, embedding_function=ef)
    seed_log.info("Created collection '%s'", coll_name)

    completed = 0
    total_chunks = 0
    failed: list[str] = []
    sem = asyncio.Semaphore(6)

    job["status"] = "indexing"
    job["message"] = f"Indexing 0/{total} council files..."

    async def process_one(file_id: str):
        nonlocal completed, total_chunks

        try:
            existing = collection.get(
                where={"council_file": file_id},
                limit=1,
                include=[],
            )
            if existing["ids"]:
                seed_log.info("Skipping %s — already indexed", file_id)
                completed += 1
                job["message"] = f"Indexing {completed}/{total} council files..."
                return
        except Exception:
            pass

        async with sem:
            try:
                pairs = await downloader.stream_and_parse(file_id)
                if not pairs:
                    seed_log.warning("%s: no text extracted", file_id)
                    failed.append(file_id)
                else:
                    pdf_tuples = [(file_id, fn, text) for fn, text in pairs]
                    n = ingest_from_memory(member_id, pdf_tuples, client, ef, collection)
                    total_chunks += n
                    seed_log.info("%s: %d chunks indexed", file_id, n)
            except Exception as e:
                seed_log.warning("%s: FAILED — %s", file_id, e)
                failed.append(file_id)

        completed += 1
        job["message"] = f"Indexing {completed}/{total} council files..."

    await asyncio.gather(*[process_one(fid) for fid in file_ids])

    if failed:
        seed_log.warning("%d files failed: %s", len(failed), failed)

    rag.invalidate_collection_cache(member_id)

    job["message"] = "Generating metadata..."
    leg_meta.generate_and_save_meta(member_id)

    job["status"] = "done"
    job["message"] = (
        f"Ready — {total_chunks} chunks indexed from "
        f"{total - len(failed)}/{total} council files"
        + (f" ({len(failed)} failed)" if failed else "")
    )
    seed_log.info("=== Indexing complete: %s ===", job["message"])


async def _run_member_seed(job_id: str, member_id: str, name: str, district: str,
                           pdf_bytes: bytes):
    job = seed_jobs[job_id]
    seed_log = get_logger(f"seed.{member_id}", log_file=f"seed_{member_id}.log")
    try:
        job["status"] = "parsing"
        job["message"] = f"Parsing PDF for {member_id}..."
        seed_log.info("=== Starting seed for %s (%s) ===", member_id, name)

        file_ids: list[str] = []
        titles: dict[str, str] = {}
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                for line in text.splitlines():
                    m = _CF_RE.search(line)
                    if m:
                        fid = m.group(1)
                        if fid not in titles:
                            file_ids.append(fid)
                            titles[fid] = line[m.end():].strip().lstrip("/ ").strip()[:200]

        if not file_ids:
            raise ValueError("No council file IDs found in the uploaded PDF.")

        seed_log.info("Parsed %d council file IDs from PDF", len(file_ids))
        files_list = [{"id": fid, "title": titles[fid]} for fid in file_ids]
        member_registry.upsert_member(member_id, name, district, files_list)

        await _run_indexing(job_id, member_id, files_list, seed_log)

    except Exception as e:
        job["status"] = "error"
        job["message"] = f"Error: {e}"
        seed_log.error("Seed failed: %s", e, exc_info=True)


async def _run_member_reseed(job_id: str, member_id: str):
    job = seed_jobs[job_id]
    seed_log = get_logger(f"seed.{member_id}", log_file=f"seed_{member_id}.log")
    try:
        seed_log.info("=== Starting reseed for %s ===", member_id)
        member = member_registry.get_member(member_id)
        files_list = member["files"]
        if not files_list:
            raise ValueError("No council files stored for this member. Upload a PDF to seed first.")
        await _run_indexing(job_id, member_id, files_list, seed_log)
    except Exception as e:
        job["status"] = "error"
        job["message"] = f"Error: {e}"
        seed_log.error("Reseed failed: %s", e, exc_info=True)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    legislations = rag.get_available_legislations()
    log.info("Health check — %d collections available", len(legislations))
    return {"status": "ok", "collections": len(legislations)}


@app.get("/api/members")
def list_members():
    """Return all members from registry with their index status."""
    members = member_registry.list_members()
    indexed_ids = {l["id"] for l in rag.get_available_legislations()}
    meta = leg_meta.load_meta()
    for m in members:
        m["indexed"] = m["id"] in indexed_ids
        m["starters"] = meta.get(m["id"], {}).get("starters", [])
        m["subtitle"] = meta.get(m["id"], {}).get("subtitle", "")
    log.info("Listing %d members", len(members))
    return members


@app.get("/api/members/{member_id}")
def get_member(member_id: str):
    member = member_registry.get_member(member_id)
    if not member:
        raise HTTPException(status_code=404, detail=f"Member '{member_id}' not found")
    indexed_ids = {l["id"] for l in rag.get_available_legislations()}
    member["indexed"] = member_id in indexed_ids
    meta = leg_meta.get_meta(member_id) or {}
    member["starters"] = meta.get("starters", [])
    member["subtitle"] = meta.get("subtitle", "")
    return member


@app.post("/api/members", response_model=MemberSeedStarted)
async def create_member(
    background_tasks: BackgroundTasks,
    member_id: str = Form(...),
    name: str = Form(...),
    district: str = Form(...),
    pdf: UploadFile = File(...),
):
    member_id = member_id.strip().lower()
    if not re.match(r"^[a-z0-9_-]+$", member_id):
        raise HTTPException(status_code=400, detail="member_id must be lowercase alphanumeric with - or _")

    # Reject if a seed job for this member is already running
    for job in seed_jobs.values():
        if job["member_id"] == member_id and job["status"] in ("parsing", "indexing"):
            raise HTTPException(status_code=409, detail=f"Seed for '{member_id}' is already running")

    pdf_bytes = await pdf.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="PDF file is empty")

    job_id = str(uuid.uuid4())
    seed_jobs[job_id] = {
        "job_id": job_id,
        "member_id": member_id,
        "status": "parsing",
        "message": f"Starting seed for {member_id}...",
    }
    log.info("Seed job %s started for member '%s' (%s)", job_id, member_id, name)

    background_tasks.add_task(_run_member_seed, job_id, member_id, name, district, pdf_bytes)
    return MemberSeedStarted(job_id=job_id, member_id=member_id)


@app.get("/api/members/{member_id}/status", response_model=MemberSeedStatus)
def member_seed_status(member_id: str):
    # Find the most recent job for this member
    matching = [j for j in seed_jobs.values() if j["member_id"] == member_id]
    if not matching:
        raise HTTPException(status_code=404, detail=f"No seed job found for '{member_id}'")
    job = max(matching, key=lambda j: j["job_id"])
    return MemberSeedStatus(**job)


@app.delete("/api/members/{member_id}")
def delete_member(member_id: str):
    member = member_registry.get_member(member_id)
    if not member:
        raise HTTPException(status_code=404, detail=f"Member '{member_id}' not found")
    chunks = rag.delete_member_collection(member_id)
    member_registry.delete_member(member_id)
    log.info("Deleted member '%s' (%d chunks removed)", member_id, chunks)
    return {"deleted": member_id, "chunks_removed": chunks}


@app.post("/api/members/{member_id}/reseed", response_model=MemberSeedStarted)
async def reseed_member(member_id: str, background_tasks: BackgroundTasks):
    """Re-index a member using the council file IDs already stored in the registry.
    Does not require re-uploading the original PDF."""
    member = member_registry.get_member(member_id)
    if not member:
        raise HTTPException(status_code=404, detail=f"Member '{member_id}' not found")
    if not member["files"]:
        raise HTTPException(
            status_code=400,
            detail=f"No council files stored for '{member_id}'. Upload a PDF to seed first.",
        )
    for job in seed_jobs.values():
        if job["member_id"] == member_id and job["status"] in ("parsing", "indexing"):
            raise HTTPException(status_code=409, detail=f"Seed for '{member_id}' is already running")

    job_id = str(uuid.uuid4())
    seed_jobs[job_id] = {
        "job_id": job_id,
        "member_id": member_id,
        "status": "indexing",
        "message": f"Starting reseed for {member_id}...",
    }
    log.info("Reseed job %s started for member '%s'", job_id, member_id)
    background_tasks.add_task(_run_member_reseed, job_id, member_id)
    return MemberSeedStarted(job_id=job_id, member_id=member_id)


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    question = req.question.strip()
    member_id = req.member_id.strip()

    log.info("Chat — member='%s' question='%s'", member_id, question)

    if not question:
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    member = member_registry.get_member(member_id)
    if not member:
        raise HTTPException(status_code=404, detail=f"Member '{member_id}' not found.")

    available = [l["id"] for l in rag.get_available_legislations()]
    if member_id not in available:
        raise HTTPException(
            status_code=400,
            detail=f"Member '{member_id}' is not yet indexed. Please seed it first.",
        )

    result = rag.answer_question(question, [member_id], session_id=req.session_id or None,
                                 client_id=req.client_id or None)
    log.info("Chat response: %d chars, %d sources, %d followups",
             len(result.get("answer", "")), len(result.get("sources", [])),
             len(result.get("followups", [])))
    return ChatResponse(
        answer=result.get("answer", ""),
        sources=result.get("sources", []),
        followups=result.get("followups", []),
    )


@app.get("/api/sessions")
def list_sessions_endpoint(client_id: str | None = None):
    from history import list_sessions
    return list_sessions(client_id=client_id)


@app.get("/api/sessions/{session_id}/messages")
def get_session_messages_endpoint(session_id: str):
    from history import get_session_messages
    return {"messages": get_session_messages(session_id)}


@app.delete("/api/sessions/{session_id}")
def delete_session_endpoint(session_id: str):
    from history import delete_session
    delete_session(session_id)
    return {"deleted": session_id}
