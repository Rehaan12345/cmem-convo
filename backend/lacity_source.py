"""
LA City Clerk source adapter.

Discovers the PDF document URLs for a council file by fetching its record page
and extracting the linked PDFs. All jurisdiction-specific logic (the record URL
shape, the HTML layout) lives here — adding another city means adding a sibling
adapter, not touching downloader.py.
"""
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from logger import get_logger

log = get_logger(__name__)

BASE_URL = "https://cityclerk.lacity.org/lacityclerkconnect/index.cfm"
USER_AGENT = "cmem-convo/1.0"


def _record_url(file_id: str) -> str:
    return f"{BASE_URL}?fa=ccfi.viewrecord&cfnumber={file_id}"


async def discover_pdf_urls(file_id: str) -> list[str]:
    """
    Fetch the council file record page and return its unique PDF document URLs.

    Raises httpx.HTTPStatusError on non-2xx (e.g. an unknown council file).
    """
    record_url = _record_url(file_id)
    log.info("Discovering PDFs for %s from %s", file_id, record_url)

    async with httpx.AsyncClient(
        timeout=60, follow_redirects=True, headers={"User-Agent": USER_AGENT}
    ) as client:
        resp = await client.get(record_url)
        resp.raise_for_status()
        html = resp.text

    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    urls: list[str] = []
    for anchor in soup.find_all("a", href=True):
        absolute = urljoin(record_url, anchor["href"].strip())
        parsed = urlparse(absolute)
        if not parsed.scheme.startswith("http"):
            continue
        if not parsed.path.lower().endswith(".pdf"):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        urls.append(absolute)

    log.info("Discovered %d PDF URLs for %s", len(urls), file_id)
    return urls
