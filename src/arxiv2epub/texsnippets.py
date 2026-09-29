"""Compile many small TeX snippets in one LaTeX run, using the paper's preamble.

Every snippet is typeset into a savebox (so we can log its height, depth and
width) and then shipped out through a ``preview`` environment, which makes each
snippet its own tightly cropped PDF page. Using the paper's own preamble means
its custom macros and packages behave exactly as in the published PDF. If that
fails (exotic class, missing files) we retry with a minimal article preamble
plus the macro definitions scraped from the source.

Used for math formulas and for pre-rendering TikZ pictures.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pymupdf

log = logging.getLogger(__name__)

BORDER_PT = 1.0

_BEGIN_DOC = re.compile(r"^[^%\n]*?\\begin\s*\{document\}", re.MULTILINE)
_DIMS = re.compile(r"^A2E-DIMS-(\d+)=([\d.]+)pt,([\d.]+)pt,([\d.]+)pt", re.MULTILINE)
_SHIPOUT = re.compile(r"\[(\d+)(?=[\]{<\s])")
_MARK = re.compile(r"^A2E-BEGIN-(\d+)$", re.MULTILINE)
_FONTSIZE = re.compile(r"^A2E-FONTSIZE=([\d.]+)", re.MULTILINE)
_XETEX_HINTS = re.compile(r"\\usepackage(\[[^\]]*\])?\{[^}]*\b(fontspec|unicode-math|xeCJK|polyglossia)\b")

MINIMAL_PREAMBLE = (
    "\\documentclass[10pt]{article}\n"
    "\\usepackage{amsmath,amssymb,amsfonts,mathtools,bm,graphicx,xcolor}\n"
)


@dataclass
class Placed:
    """Where a compiled snippet ended up and how big it is (in pt)."""

    pdf: Path
    page: int  # 0-based
    height: float
    depth: float
    width: float
    fontsize: float


# ------------------------------------------------------------ preamble helpers

def strip_comments(text: str) -> str:
    return re.sub(r"(?<!\\)%.*", "", text)


def split_document(text: str) -> tuple[str, str]:
    """Split TeX source into (preamble, rest-starting-at-\\begin{document})."""
    m = _BEGIN_DOC.search(text)
    return (text[: m.start()], text[m.start():]) if m else (text, "")


def extract_preamble(main_tex: Path) -> str:
    return split_document(main_tex.read_text(errors="replace"))[0]


def resolve_inputs(text: str, root: Path, depth: int = 0) -> str:
    """Inline \\input/\\include'd files (for scraping macros from the preamble)."""
    if depth > 5:
        return text

    def repl(m: re.Match) -> str:
        name = m.group(2).strip()
        for cand in (root / name, root / f"{name}.tex"):
            if cand.is_file():
                return resolve_inputs(strip_comments(cand.read_text(errors="replace")), root, depth + 1)
        return ""

    return re.sub(r"\\(input|include)\s*\{([^}]*)\}", repl, text)


_DEF_CMDS = re.compile(
    r"\\(newcommand|renewcommand|providecommand|DeclareMathOperator|DeclareRobustCommand"
    r"|def|newenvironment|let|DeclareMathAlphabet|newtheorem)\b\*?"
)


def _read_group(s: str, i: int) -> int:
    """Given s[i] == '{', return index just past the matching '}'."""
    depth = 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(s)


def scrape_macros(preamble: str) -> str:
    """Pull macro definitions out of a preamble, with balanced-brace bodies."""
    s = strip_comments(preamble)
    out = []
    for m in _DEF_CMDS.finditer(s):
        i = m.end()
        cmd = m.group(1)
        if cmd == "let":
            end = s.find("\n", i)
            out.append(s[m.start(): end if end != -1 else len(s)])
            continue
        n_groups = {"newenvironment": 3, "DeclareMathAlphabet": 5, "newtheorem": 2}.get(cmd, 2)
        if cmd == "def":
            n_groups = 1
            while i < len(s) and s[i] != "{":  # \def\foo#1#2{...}
                i += 1
        groups = 0
        while i < len(s) and groups < n_groups:
            c = s[i]
            if c.isspace() or c == "*":
                i += 1
            elif c == "{":
                i = _read_group(s, i)
                groups += 1
            elif c == "[":
                j = s.find("]", i)
                i = j + 1 if j != -1 else len(s)
            elif c == "\\" and groups == 0:  # \newcommand\foo{...}
                j = i + 1
                while j < len(s) and s[j].isalpha():
                    j += 1
                i = max(j, i + 2)
                groups += 1
            else:
                break
        out.append(s[m.start(): i])
    return "\n".join(out)


def engine_for(preamble: str) -> str:
    return "xelatex" if _XETEX_HINTS.search(strip_comments(preamble)) else "pdflatex"


# ------------------------------------------------------------ compiling

def _document(preamble: str, bodies: list[str], setup: str) -> str:
    parts = [
        preamble,
        r"\RequirePackage{amsmath}",
        r"\usepackage[active,tightpage]{preview}",
        rf"\setlength\PreviewBorder{{{BORDER_PT}pt}}",
        r"\newsavebox\aeBox",
        r"\begin{document}",
        r"\makeatletter\typeout{A2E-FONTSIZE=\f@size}\makeatother",
        setup,
    ]
    for i, body in enumerate(bodies, 1):
        parts.append(
            rf"\typeout{{A2E-BEGIN-{i}}}\sbox\aeBox{{{body}}}"
            rf"\typeout{{A2E-DIMS-{i}=\the\ht\aeBox,\the\dp\aeBox,\the\wd\aeBox}}"
            r"\begin{preview}\usebox\aeBox\end{preview}"
        )
    parts.append(r"\typeout{A2E-BEGIN-END}")
    parts.append(r"\end{document}")
    return "\n".join(parts)


def parse_log(log_text: str) -> dict[int, tuple[int, float, float, float]]:
    """Map snippet number -> (pdf page number, height, depth, width) for clean snippets."""
    marks = [(m.start(), int(m.group(1))) for m in _MARK.finditer(log_text)]
    end = log_text.find("A2E-BEGIN-END")
    bounds = marks + [(end if end != -1 else len(log_text), None)]
    dims = {int(m.group(1)): tuple(float(m.group(g)) for g in (2, 3, 4)) for m in _DIMS.finditer(log_text)}
    out = {}
    for (start, n), (stop, _) in zip(bounds, bounds[1:]):
        region = log_text[start:stop]
        if re.search(r"^! ", region, re.MULTILINE) or n not in dims:
            continue
        pages = _SHIPOUT.findall(region)
        if len(pages) != 1:
            continue
        out[n] = (int(pages[0]), *dims[n])
    return out


def _compile(doc: str, n_bodies: int, src_dir: Path, out_dir: Path, job: str,
             engine: str, timeout: int) -> dict[int, Placed]:
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = src_dir / f"{job}.tex"
    tex_path.write_text(doc)
    try:
        subprocess.run(
            [engine, "-interaction=nonstopmode", f"-output-directory={out_dir}", f"{job}.tex"],
            cwd=src_dir, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("%s compile timed out", job)
        return {}
    finally:
        tex_path.unlink(missing_ok=True)
    log_path, pdf_path = out_dir / f"{job}.log", out_dir / f"{job}.pdf"
    if not pdf_path.exists() or not log_path.exists():
        return {}
    log_text = log_path.read_text(errors="replace")
    fs = _FONTSIZE.search(log_text)
    fontsize = float(fs.group(1)) if fs else 10.0
    with pymupdf.open(pdf_path) as pdf:
        n_pages = pdf.page_count
    return {
        k - 1: Placed(pdf_path, page - 1, h, d, w, fontsize)
        for k, (page, h, d, w) in parse_log(log_text).items()
        if k <= n_bodies and page <= n_pages
    }


def build_aux(main_tex: Path, build_dir: Path, timeout: int = 600) -> Path | None:
    """Compile the whole paper once (draft mode) to get its .aux file.

    Snippet documents load it so \\cite, \\ref and \\eqref inside formulas,
    tables and pictures resolve to the same numbers as in the paper.
    """
    preamble = extract_preamble(main_tex)
    engine = engine_for(preamble)
    if shutil.which(engine) is None:
        return None
    build_dir.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            [engine, "-interaction=nonstopmode", "-draftmode",
             f"-output-directory={build_dir}", main_tex.name],
            cwd=main_tex.parent, capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning("full-paper compile timed out; citations in tables may show as [?]")
        return None
    aux = build_dir / f"{main_tex.stem}.aux"
    return aux if aux.exists() and aux.stat().st_size > 0 else None


def compile_snippets(
    bodies: list[str], main_tex: Path, build_dir: Path, job: str,
    setup: str = "", aux: Path | None = None, timeout: int = 600,
) -> list[Placed | None]:
    """Typeset each body (horizontal-mode TeX) to its own PDF page. Failures are None.

    ``aux`` (from :func:`build_aux`) is loaded when using the paper's preamble.
    """
    results: list[Placed | None] = [None] * len(bodies)
    if not bodies:
        return results
    src_dir = main_tex.parent
    preamble = extract_preamble(main_tex)
    # Read in the preamble: LaTeX forbids \newlabel after \begin{document}.
    # \@input silently skips a missing file.
    load_aux = f"\n\\makeatletter\\@input{{{aux}}}\\makeatother" if aux else ""
    attempts = [
        ("paper", preamble + load_aux, engine_for(preamble)),
        ("minimal", MINIMAL_PREAMBLE + scrape_macros(resolve_inputs(preamble, src_dir)), "pdflatex"),
    ]
    todo = list(range(len(bodies)))
    for label, pre, engine in attempts:
        if not todo:
            break
        if shutil.which(engine) is None:
            log.warning("%s not found; skipping %s-preamble attempt", engine, label)
            continue
        subset = [bodies[i] for i in todo]
        good = _compile(_document(pre, subset, setup), len(subset), src_dir,
                        build_dir / label, job, engine, timeout)
        log.info("%s (%s preamble): %d/%d snippets compiled", job, label, len(good), len(subset))
        for local_i, placed in good.items():
            results[todo[local_i]] = placed
        todo = [i for i in todo if results[i] is None]
    return results
