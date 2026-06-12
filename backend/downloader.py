"""
Downloads and parses council file PDFs directly from the source.

stream_and_parse() is the primary entry point — discovers a council file's PDF
URLs via the jurisdiction source adapter, downloads each PDF into memory, parses
text with pdfplumber, and returns (filename, text) pairs. No files are written to
disk.
"""
import asyncio
import io
import posixpath
import re
from urllib.parse import urlparse

import httpx
import pdfplumber

import lacity_source
from logger import get_logger

log = get_logger(__name__)

USER_AGENT = "cmem-convo/1.0"


def _sanitize_filename(filename: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
    return cleaned or "document.pdf"


def _unique_filenames(filenames: list[str]) -> list[str]:
    """Disambiguate duplicate basenames within one council file (foo.pdf, foo-2.pdf)."""
    seen: dict[str, int] = {}
    unique: list[str] = []
    for filename in filenames:
        count = seen.get(filename, 0) + 1
        seen[filename] = count
        if count == 1:
            unique.append(filename)
            continue
        stem, dot, suffix = filename.rpartition(".")
        if not dot:
            stem, suffix = filename, ""
        unique.append(f"{stem}-{count}.{suffix}" if suffix else f"{stem}-{count}")
    return unique


async def _download_pdf(client: httpx.AsyncClient, url: str) -> tuple[str, bytes]:
    resp = await client.get(url)
    resp.raise_for_status()
    raw_name = posixpath.basename(urlparse(str(resp.url)).path) or "document.pdf"
    return _sanitize_filename(raw_name), resp.content


async def stream_and_parse(file_id: str) -> list[tuple[str, str]]:
    """
    Discover the council file's PDFs, download each into memory, parse text with
    pdfplumber, and return [(filename, text), ...].

    Raises httpx.HTTPStatusError on non-2xx during discovery (e.g. 404).
    """
    pdf_urls = await lacity_source.discover_pdf_urls(file_id)
    if not pdf_urls:
        log.warning("No PDFs found for %s", file_id)
        return []

    async with httpx.AsyncClient(
        timeout=300, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        downloads = await asyncio.gather(*(_download_pdf(client, u) for u in pdf_urls))

    filenames = _unique_filenames([name for name, _ in downloads])

    results: list[tuple[str, str]] = []
    for (_, content), filename in zip(downloads, filenames):
        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages).strip()
            if text:
                results.append((filename, text))
            else:
                log.warning("  No text extracted from %s/%s", file_id, filename)
        except Exception as e:
            log.warning("  Could not parse %s/%s: %s", file_id, filename, e)

    log.info("Parsed %d/%d PDFs with text for %s", len(results), len(pdf_urls), file_id)
    return results
