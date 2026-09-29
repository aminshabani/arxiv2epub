"""Pre-render TikZ pictures and wide tables with real LaTeX before LaTeXML.

* Pictures: LaTeXML interprets pgf in Perl, which is extremely slow (a few
  pgfplots axes can take >10 minutes) and often inaccurate, and it can only
  turn ``picture``/``overpic`` into images with ImageMagick. Every
  ``tikzpicture``, ``picture`` and ``overpic`` is typeset with pdflatex instead.
* Tables: e-readers can't scroll horizontally, so a table wider than a
  screen gets clipped. Each ``tabular`` is typeset and measured; the ones wider
  than ``MAX_TABLE_EM`` become images (readers can long-press to zoom), the rest
  stay as reflowable HTML.

Rendered pieces are rasterized and swapped into the source as
``\\includegraphics`` so LaTeXML only sees an image.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .images import FIGURE_DPI, MAX_WIDTH, downscale, safe_dpi, shrink
from .texsnippets import _read_group, compile_snippets, scrape_macros, split_document, strip_comments

log = logging.getLogger(__name__)

OUT_SUBDIR = "a2e_img"
MAX_TABLE_EM = 30  # roughly a Kindle line at a typical font size
MIN_IMAGE_COLUMNS = 3  # tables with fewer columns wrap fine as HTML, keep them as text

PICTURE_ENVS = ("tikzpicture", "picture", "overpic", "Overpic")
TABLE_ENVS = ("tabular", "tabular*", "tabularx", "tabulary")

# Body-level commands a picture may depend on (tables read earlier, styles, colours).
_SETUP_CMDS = re.compile(
    r"\\(pgfplotstableread|pgfplotsset|tikzset|definecolor|colorlet|usetikzlibrary"
    r"|usepgfplotslibrary|tikzstyle)\b"
)
# Number of mandatory arguments for each setup command.
_SETUP_ARITY = {
    "pgfplotstableread": 2, "pgfplotsset": 1, "tikzset": 1, "definecolor": 3,
    "colorlet": 2, "usetikzlibrary": 1, "usepgfplotslibrary": 1, "tikzstyle": 1,
}


def _commented(text: str, pos: int) -> bool:
    line_start = text.rfind("\n", 0, pos) + 1
    return re.search(r"(?<!\\)%", text[line_start:pos]) is not None


def find_envs(text: str, names: tuple[str, ...]) -> list[tuple[int, int]]:
    """(start, end) spans of top-level environments in ``names``, skipping comments.

    Environments in the same family nest (a tabular inside a tabular belongs to
    the outer one).
    """
    alt = "|".join(re.escape(n) for n in names)
    begin = re.compile(rf"\\begin\s*\{{({alt})\}}")
    anyre = re.compile(rf"\\(begin|end)\s*\{{({alt})\}}")
    spans = []
    pos = 0
    while (m := begin.search(text, pos)) is not None:
        if _commented(text, m.start()):
            pos = m.end()
            continue
        depth, j = 0, m.start()
        for t in anyre.finditer(text, m.start()):
            if _commented(text, t.start()):
                continue
            depth += 1 if t.group(1) == "begin" else -1
            if depth == 0:
                j = t.end()
                break
        else:
            break  # unbalanced; leave the rest alone
        spans.append((m.start(), j))
        pos = j
    return spans


def find_pictures(text: str) -> list[tuple[int, int]]:
    return find_envs(text, PICTURE_ENVS)


def _setup_commands(text: str) -> list[str]:
    out = []
    for m in _SETUP_CMDS.finditer(text):
        if _commented(text, m.start()):
            continue
        name, i = m.group(1), m.end()
        for _ in range(_SETUP_ARITY[name]):
            while i < len(text) and text[i] in " \t\n":
                i += 1
            if text.startswith("[", i):  # optional argument
                j = text.find("]", i)
                i = j + 1 if j != -1 else i
            if text.startswith("{", i):
                i = _read_group(text, i)
            elif text.startswith("\\", i):  # e.g. \pgfplotstableread{file}\table
                j = i + 1
                while j < len(text) and text[j].isalpha():
                    j += 1
                i = j
            else:
                break
        if name == "tikzstyle":  # \tikzstyle{x}=[...]
            eq = re.match(r"\s*=\s*\[", text[i:])
            if eq:
                j = text.find("]", i + eq.end())
                i = j + 1 if j != -1 else i
        out.append(text[m.start():i])
    return out


def count_columns(env_text: str) -> int:
    """Count columns in a tabular's column spec, e.g. ``{l|c@{}p{3cm}*{2}{r}}`` -> 5."""
    m = re.match(r"\\begin\s*\{(tabular\*?|tabularx|tabulary)\}\s*", env_text)
    if not m:
        return 0
    i = m.end()
    if m.group(1) != "tabular":  # width argument first
        if env_text.startswith("{", i):
            i = _read_group(env_text, i)
    if env_text.startswith("[", i):  # [t] position
        i = env_text.find("]", i) + 1
    if not env_text.startswith("{", i):
        return 0
    spec = env_text[i + 1:_read_group(env_text, i) - 1]

    def count(spec: str) -> int:
        n, j = 0, 0
        while j < len(spec):
            c = spec[j]
            if c in "@!><":  # @{..} !{..} >{..} <{..}: not columns
                j += 1
                if spec.startswith("{", j):
                    j = _read_group(spec, j)
                continue
            if c == "*":  # *{n}{spec}
                j += 1
                if spec.startswith("{", j):
                    k = _read_group(spec, j)
                    reps = spec[j + 1:k - 1].strip()
                    j = k
                    if spec.startswith("{", j):
                        k = _read_group(spec, j)
                        n += (int(reps) if reps.isdigit() else 1) * count(spec[j + 1:k - 1])
                        j = k
                continue
            if c.isalpha():
                n += 1
                j += 1
                if spec.startswith("{", j):  # p{3cm}, m{..}, custom x{42}
                    j = _read_group(spec, j)
                continue
            j += 1
        return n

    return count(spec)


@dataclass
class _Piece:
    file: Path
    start: int
    end: int
    kind: str  # "picture" | "table"


def _collect(main_tex: Path, texts: dict[Path, str]) -> tuple[list[_Piece], list[str]]:
    pieces: list[_Piece] = []
    setup: list[str] = []
    for p, text in texts.items():
        body_start = len(split_document(text)[0]) if p == main_tex else 0

        def usable(span: tuple[int, int], text=text, body_start=body_start) -> bool:
            # '#' means we're inside a macro definition: leave those alone.
            return span[0] >= body_start and "#" not in text[span[0]:span[1]]

        pics = [s for s in find_envs(text, PICTURE_ENVS) if usable(s)]
        tables = [
            s for s in find_envs(text, TABLE_ENVS)
            if usable(s) and not any(a <= s[0] < b for a, b in pics)
            and count_columns(text[s[0]:s[1]]) >= MIN_IMAGE_COLUMNS
        ]
        pieces += [_Piece(p, a, b, "picture") for a, b in pics]
        pieces += [_Piece(p, a, b, "table") for a, b in tables]
        rest, last = [], body_start
        for a, b in sorted(pics + tables):
            if a < last:
                continue
            rest.append(text[last:a])
            last = b
        rest.append(text[last:])
        outside = "".join(rest)
        # Macros defined in the body (often right next to a table) and pgf setup.
        setup.append(scrape_macros(outside))
        setup += _setup_commands(outside)
    return pieces, setup


def prerender(main_tex: Path, build_dir: Path, aux: Path | None = None,
              timeout: int = 600) -> dict[str, int]:
    """Replace pictures (TikZ, picture, overpic) and wide tables in the source with images.

    Returns counts: {"pictures": n, "tables": n, "failed": n}.
    """
    src_dir = main_tex.parent
    files = sorted(p for p in src_dir.rglob("*.tex") if not p.name.startswith("a2e_"))
    texts = {p: p.read_text(errors="replace") for p in files}
    pieces, setup = _collect(main_tex, texts)
    counts = {"pictures": 0, "tables": 0, "failed": 0}
    if not pieces:
        return counts

    log.info("typesetting %d pictures / tables", len(pieces))
    placed = compile_snippets(
        [texts[pc.file][pc.start:pc.end] for pc in pieces], main_tex, build_dir,
        job="a2e_pre", setup="\n".join(setup), aux=aux, timeout=timeout,
    )

    out_dir = src_dir / OUT_SUBDIR
    out_dir.mkdir(exist_ok=True)
    replacements: dict[Path, list[tuple[int, int, str]]] = {}
    docs: dict[Path, pymupdf.Document] = {}
    try:
        for i, (pc, pl) in enumerate(zip(pieces, placed)):
            if pc.kind == "table" and (pl is None or pl.width / pl.fontsize <= MAX_TABLE_EM):
                continue  # narrow (or unmeasurable) table: LaTeXML keeps it as HTML
            if pl is None:
                counts["failed"] += 1
                repl = r"\textit{[figure could not be rendered]}"
            else:
                doc = docs.setdefault(pl.pdf, pymupdf.open(pl.pdf))
                page = doc[pl.page]
                png = out_dir / f"{pc.kind}{i}.png"
                page.get_pixmap(dpi=safe_dpi(page.rect, FIGURE_DPI, MAX_WIDTH), alpha=False).save(png)
                downscale(png)
                final = shrink(png)
                # TeX resolves graphics paths from the main file's directory.
                rel = final.relative_to(src_dir).as_posix()
                repl = rf"\includegraphics[width={page.rect.width:.1f}pt]{{{rel}}}"
                counts["pictures" if pc.kind == "picture" else "tables"] += 1
            replacements.setdefault(pc.file, []).append((pc.start, pc.end, repl))
    finally:
        for d in docs.values():
            d.close()

    for p, reps in replacements.items():
        text = texts[p]
        for a, b, repl in sorted(reps, reverse=True):
            text = text[:a] + repl + text[b:]
        p.write_text(text)
        texts[p] = text

    # LaTeXML needs graphicx for \includegraphics.
    pre, rest = split_document(texts[main_tex])
    if replacements and not re.search(r"\\usepackage(\[[^\]]*\])?\{[^}]*\bgraphicx\b", strip_comments(pre)):
        main_tex.write_text(pre + "\\usepackage{graphicx}\n" + rest)
    if counts["failed"]:
        log.warning("%d pictures could not be rendered", counts["failed"])
    return counts
