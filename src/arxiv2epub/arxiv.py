"""arXiv ID parsing, metadata lookup and source download."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import httpx

USER_AGENT = "arxiv2epub/0.1 (personal e-reader conversion tool)"
CACHE_DIR = Path.home() / ".cache" / "arxiv2epub"

_NEW_ID = r"\d{4}\.\d{4,5}(?:v\d+)?"
_OLD_ID = r"[a-z\-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"
_ID_RE = re.compile(rf"^(?:{_NEW_ID}|{_OLD_ID})$")
_URL_RE = re.compile(
    rf"arxiv\.org/(?:abs|pdf|html|src|format)/({_NEW_ID}|{_OLD_ID})", re.IGNORECASE
)

_ATOM = {"a": "http://www.w3.org/2005/Atom"}


class ArxivError(Exception):
    pass


def parse_id(text: str) -> str:
    """Normalize an arXiv ID or URL to a bare ID (keeping any version suffix)."""
    text = text.strip()
    m = _URL_RE.search(text)
    if m:
        text = m.group(1)
    text = re.sub(r"^arxiv:", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\.pdf$", "", text)
    if not _ID_RE.match(text):
        raise ArxivError(f"Not a valid arXiv ID or URL: {text!r}")
    return text


def safe_name(arxiv_id: str) -> str:
    """Filesystem-safe form of an ID (old-style IDs contain '/')."""
    return arxiv_id.replace("/", "_")


@dataclass
class Metadata:
    arxiv_id: str
    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    published: str = ""


def _client() -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=60
    )


def _get(client: httpx.Client, url: str, **kw) -> httpx.Response:
    """GET with retries on arXiv's rate limiting (429) and transient 5xx errors."""
    for attempt in range(5):
        try:
            r = client.get(url, **kw)
        except httpx.TransportError as e:
            if attempt == 4:
                raise ArxivError(f"Network error talking to arXiv: {e}") from e
        else:
            if r.status_code not in (429, 500, 502, 503, 504):
                return r
            if attempt == 4:
                raise ArxivError(f"arXiv is not responding (HTTP {r.status_code}); try again later.")
        time.sleep(3 * 2**attempt)  # arXiv asks for >= 3s between requests
    raise AssertionError("unreachable")


def fetch_metadata(arxiv_id: str) -> Metadata:
    with _client() as c:
        r = _get(c, "https://export.arxiv.org/api/query", params={"id_list": arxiv_id})
        if r.is_error:
            raise ArxivError(f"arXiv API error (HTTP {r.status_code})")
    root = ET.fromstring(r.text)
    entry = root.find("a:entry", _ATOM)
    title_el = entry.find("a:title", _ATOM) if entry is not None else None
    if title_el is None or not (title_el.text or "").strip():
        raise ArxivError(f"arXiv has no paper with ID {arxiv_id}")

    def clean(s: str | None) -> str:
        return " ".join((s or "").split())

    return Metadata(
        arxiv_id=arxiv_id,
        title=clean(title_el.text),
        authors=[clean(a.findtext("a:name", "", _ATOM)) for a in entry.findall("a:author", _ATOM)],
        abstract=clean(entry.findtext("a:summary", "", _ATOM)),
        published=clean(entry.findtext("a:published", "", _ATOM))[:10],
    )


def download_source(arxiv_id: str, use_cache: bool = True) -> Path:
    """Download the source bundle for a paper and return its path."""
    dest = CACHE_DIR / safe_name(arxiv_id) / "source.bin"
    if use_cache and dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    with _client() as c:
        r = _get(c, f"https://arxiv.org/src/{arxiv_id}")
    if r.status_code == 404:
        raise ArxivError(f"No source available for {arxiv_id}")
    if r.is_error:
        raise ArxivError(f"Downloading the source failed (HTTP {r.status_code})")
    tmp.write_bytes(r.content)
    tmp.replace(dest)
    return dest
