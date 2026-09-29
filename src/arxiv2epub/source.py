"""Unpacking arXiv source bundles and locating the main .tex file."""

from __future__ import annotations

import gzip
import io
import re
import shutil
import tarfile
from pathlib import Path


# Limits for untrusted bundles (the largest real arXiv sources are a few hundred MB).
MAX_UNPACKED_BYTES = 1 << 30
MAX_MEMBERS = 10_000


class SourceError(Exception):
    pass


def _gunzip(data: bytes) -> bytes:
    """Decompress gzip data, refusing to expand past ``MAX_UNPACKED_BYTES``."""
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as f:
        out = f.read(MAX_UNPACKED_BYTES + 1)
    if len(out) > MAX_UNPACKED_BYTES:
        raise SourceError(f"Source bundle expands to more than {MAX_UNPACKED_BYTES >> 20} MB; refusing.")
    return out


def _check_tar(t: tarfile.TarFile) -> None:
    members = t.getmembers()
    if len(members) > MAX_MEMBERS:
        raise SourceError(f"Source bundle has more than {MAX_MEMBERS} files; refusing.")
    if sum(m.size for m in members if m.isfile()) > MAX_UNPACKED_BYTES:
        raise SourceError(f"Source bundle expands to more than {MAX_UNPACKED_BYTES >> 20} MB; refusing.")


def detect_kind(data: bytes) -> str:
    """Return one of 'tar.gz', 'gz', 'tar', 'pdf', 'tex'."""
    if data.startswith(b"%PDF"):
        return "pdf"
    if data[:2] == b"\x1f\x8b":
        inner = _gunzip(data)
        if _is_tar(inner):
            return "tar.gz"
        if inner.startswith(b"%PDF"):
            return "pdf"
        return "gz"
    if _is_tar(data):
        return "tar"
    return "tex"


def _is_tar(data: bytes) -> bool:
    return len(data) > 262 and data[257:262] == b"ustar" or _tar_opens(data)


def _tar_opens(data: bytes) -> bool:
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            return t.next() is not None
    except tarfile.TarError:
        return False


def extract(bundle: Path, dest: Path) -> Path:
    """Extract a source bundle into ``dest`` (recreated) and return it."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    data = bundle.read_bytes()
    kind = detect_kind(data)
    if kind == "pdf":
        raise SourceError(
            "This paper was submitted as a PDF only; there is no LaTeX source to convert."
        )
    if kind in ("tar.gz", "tar"):
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            _check_tar(t)
            t.extractall(dest, filter="data")  # rejects path traversal / links out
    else:
        text = _gunzip(data) if kind == "gz" else data
        (dest / "main.tex").write_bytes(text)
    return dest


_DOCCLASS = re.compile(rb"^[^%\n]*\\document(?:class|style)", re.MULTILINE)
_BEGIN_DOC = re.compile(rb"^[^%\n]*\\begin\s*\{document\}", re.MULTILINE)
_PREFERRED = ("main", "ms", "paper", "article", "arxiv")


def find_main_tex(root: Path) -> Path:
    candidates = []
    for p in root.rglob("*.tex"):
        data = p.read_bytes()
        if _DOCCLASS.search(data):
            candidates.append((bool(_BEGIN_DOC.search(data)), p))
    if not candidates:
        if any(re.search(rb"\\input\s*harvmac|\\bye\b", p.read_bytes()) for p in root.rglob("*.tex")):
            raise SourceError(
                "This paper is written in plain TeX (e.g. harvmac), not LaTeX; "
                "LaTeXML can't convert it."
            )
        raise SourceError("Could not find a .tex file with \\documentclass in the source.")

    def score(item: tuple[bool, Path]):
        has_begin, p = item
        return (
            has_begin,
            p.stem.lower() in _PREFERRED,
            -len(p.relative_to(root).parts),  # prefer top-level files
            p.stat().st_size,
        )

    return max(candidates, key=score)[1]


def check_convertible(main_tex: Path) -> None:
    """Reject sources that merely wrap a PDF (\\includepdf) instead of real LaTeX."""
    text = re.sub(r"(?<!\\)%.*", "", main_tex.read_text(errors="replace"))
    m = re.search(r"\\begin\s*\{document\}(.*?)(\\end\s*\{document\}|$)", text, re.DOTALL)
    body = m.group(1) if m else ""
    if "\\includepdf" in body and len(re.sub(r"\\includepdf(\[[^\]]*\])?\{[^}]*\}|\s", "", body)) < 200:
        raise SourceError(
            "This paper's LaTeX source just embeds a PDF (\\includepdf); "
            "there is no real LaTeX to convert."
        )
