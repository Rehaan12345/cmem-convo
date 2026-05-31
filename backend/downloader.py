"""
Downloads council file PDFs from the scrape-cf API.

stream_and_parse() is the primary entry point — downloads a council file ZIP,
extracts PDFs entirely in memory, parses text with pdfplumber, and returns
(filename, text) pairs. No files are written to disk.
"""
import asyncio
import io
import zipfile
from pathlib import Path

import httpx
import pdfplumber

from logger import get_logger

log = get_logger(__name__)

SCRAPE_API = "https://scrape-cf.vercel.app"


async def stream_and_parse(file_id: str) -> list[tuple[str, str]]:
    """
    Download the ZIP for `file_id` from scrape-cf, extract each PDF into memory,
    parse text with pdfplumber, and return [(filename, text), ...].

    Raises httpx.HTTPStatusError on non-2xx (e.g. 404).
    """
    url = f"{SCRAPE_API}/council-files/{file_id}/pdfs"
    log.info("Streaming %s from scrape-cf...", file_id)

    zip_buffer = io.BytesIO()
    async with httpx.AsyncClient(timeout=300) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))
            downloaded = 0
            async for chunk in response.aiter_bytes(chunk_size=65536):
                zip_buffer.write(chunk)
                downloaded += len(chunk)
                if total and downloaded % (1024 * 1024) < 65536:
                    log.info("  %s: %d/%d bytes (%.0f%%)",
                             file_id, downloaded, total, downloaded / total * 100)

    results: list[tuple[str, str]] = []
    with zipfile.ZipFile(zip_buffer) as zf:
        pdf_names = [n for n in zf.namelist() if n.lower().endswith(".pdf")]
        log.info("Extracted %d PDFs from %s zip", len(pdf_names), file_id)
        for name in pdf_names:
            filename = Path(name).name
            try:
                pdf_bytes = io.BytesIO(zf.read(name))
                with pdfplumber.open(pdf_bytes) as pdf:
                    text = "\n".join(page.extract_text() or "" for page in pdf.pages).strip()
                if text:
                    results.append((filename, text))
                else:
                    log.warning("  No text extracted from %s/%s", file_id, filename)
            except Exception as e:
                log.warning("  Could not parse %s/%s: %s", file_id, filename, e)

    log.info("Parsed %d/%d PDFs with text for %s", len(results), len(pdf_names), file_id)
    return results


async def probe_branch_exists(base_file: str, n: int) -> bool:
    cf = f"{base_file}-S{n}"
    url = f"{SCRAPE_API}/council-files/{cf}/pdf-links"
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.get(url)
            if resp.status_code == 404:
                return False
            data = resp.json()
            return data.get("unique_pdf_count", 0) > 0
        except Exception:
            return False
