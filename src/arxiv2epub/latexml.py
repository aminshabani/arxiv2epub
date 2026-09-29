"""Running LaTeXML in two stages, with our own figure conversion in between.

LaTeXML needs Perl's Image::Magick to convert PDF/EPS figures, which Homebrew's
LaTeXML doesn't ship. So we run ``latexml`` (TeX -> XML), rasterize any
non-web figures ourselves and point the XML at the PNGs, then run
``latexmlpost`` (XML -> HTML5 with MathML).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from lxml import etree

from .images import rasterize_figure

log = logging.getLogger(__name__)

LTX_NS = "http://dlmf.nist.gov/LaTeXML"
XML_ID = "{http://www.w3.org/XML/1998/namespace}id"
WEB_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif"}
CONVERTIBLE_EXTS = {".pdf", ".eps", ".ps", ".svg"}


class LatexmlError(Exception):
    pass


@dataclass
class LatexmlResult:
    html: Path
    errors: int
    warnings: int
    log_path: Path


def _run(cmd: list[str], cwd: Path, timeout: int, log_path: Path) -> None:
    """Run a LaTeXML command, streaming its output into ``log_path``."""
    log.debug("running %s", " ".join(cmd))
    with log_path.open("a") as f:
        try:
            proc = subprocess.run(cmd, cwd=cwd, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise LatexmlError(f"{cmd[0]} timed out after {timeout}s") from e
    if proc.returncode not in (0, 1, 2):  # 1/2 = warnings/errors but output produced
        raise LatexmlError(f"{cmd[0]} failed (exit {proc.returncode}):\n{_fatal_summary(log_path)}")


def _count(log_path: Path) -> tuple[int, int]:
    text = log_path.read_text(errors="replace")
    return (
        len(re.findall(r"^Error:", text, re.MULTILINE)),
        len(re.findall(r"^Warning:", text, re.MULTILINE)),
    )


_PICTURE_IMAGE = re.compile(r"\\begin\{[Oo]verpic\}(?:\[[^\]]*\])?\{([^}]*)\}")


def replace_pictures(tree: etree._ElementTree) -> int:
    """Swap leftover <picture> elements for their base image, or drop them.

    latexmlpost can only rasterize pictures with Perl's Image::Magick; without it
    the whole post-processing run silently produces no output. Pictures are
    normally pre-rendered (see prerender.py), so this is only a safety net.
    """
    n = 0
    for pic in list(tree.iter(f"{{{LTX_NS}}}picture")):
        m = _PICTURE_IMAGE.search(pic.get("tex") or "")
        parent = pic.getparent()
        if m:
            g = etree.Element(f"{{{LTX_NS}}}graphics", graphic=m.group(1), candidates=m.group(1))
            if pic.get(XML_ID):
                g.set(XML_ID, pic.get(XML_ID))
            g.tail = pic.tail
            parent.replace(pic, g)
        else:
            if pic.tail:
                prev = pic.getprevious()
                if prev is not None:
                    prev.tail = (prev.tail or "") + pic.tail
                else:
                    parent.text = (parent.text or "") + pic.tail
            parent.remove(pic)
        n += 1
    return n


def _resolve_candidates(g: etree._Element, src_dir: Path) -> None:
    """Fill in candidates for a graphic given without extension (our picture fallback)."""
    if g.get("candidates"):
        cand = g.get("candidates")
        if (src_dir / cand).exists() or Path(cand).suffix:
            return
    base = g.get("graphic") or ""
    for ext in (".png", ".jpg", ".jpeg", ".pdf", ".eps"):
        if (src_dir / f"{base}{ext}").exists():
            g.set("candidates", f"{base}{ext}")
            return


def convert_figures(xml_path: Path, src_dir: Path) -> int:
    """Rasterize PDF/EPS/SVG graphics referenced by the XML; returns count converted."""
    tree = etree.parse(str(xml_path))
    if n := replace_pictures(tree):
        log.warning("%d picture environments could not be pre-rendered; kept base images only", n)
    converted = 0
    for g in tree.iter(f"{{{LTX_NS}}}graphics"):
        _resolve_candidates(g, src_dir)
        cands = [c for c in (g.get("candidates") or "").split(",") if c]
        if not cands or any(Path(c).suffix.lower() in WEB_IMAGE_EXTS for c in cands):
            continue
        cand = next((c for c in cands if Path(c).suffix.lower() in CONVERTIBLE_EXTS), None)
        if cand is None or not (src_dir / cand).exists():
            continue
        out_rel = str(Path(cand).with_suffix("")) + ".a2e.png"
        try:
            written = rasterize_figure(src_dir / cand, src_dir / out_rel, g.get("options") or "")
            out_rel = str(written.relative_to(src_dir))
        except Exception as e:  # noqa: BLE001 - one bad figure shouldn't kill the book
            log.warning("could not convert figure %s: %s", cand, e)
            continue
        g.set("candidates", out_rel)
        g.set("graphic", out_rel)
        # Trim/clip were applied while rasterizing; drop them so nothing reapplies them.
        opts = g.get("options") or ""
        opts = ",".join(o for o in opts.split(",") if not o.startswith(("trim", "clip", "viewport", "bb")))
        g.set("options", opts)
        converted += 1
    tree.write(str(xml_path), xml_declaration=True, encoding="UTF-8")
    return converted


STUBS_DIR = Path(__file__).parent / "assets" / "stubs"

# Optional stub bindings: used only when the source doesn't need the real thing.
_NEEDS_REAL = {
    # real pgf is needed for TikZ that wasn't pre-rendered (e.g. tikzcd inside math)
    "tikz": re.compile(r"\\begin\s*\{(tikzpicture|tikzcd)\}|\\tikz\b(?!set|style|external)"),
    # real expl3 is needed if the paper itself programs in expl3
    "expl3": re.compile(r"\\ExplSyntaxOn"),
}


def stub_paths(src_dir: Path) -> list[Path]:
    texts = [
        re.sub(r"(?<!\\)%.*", "", p.read_text(errors="replace"))
        for p in src_dir.rglob("*") if p.suffix in (".tex", ".sty", ".cls")
    ]
    paths = [STUBS_DIR / "common"]
    for name, pattern in _NEEDS_REAL.items():
        if not any(pattern.search(t) for t in texts):
            paths.append(STUBS_DIR / name)
    return paths


_EXPL3_SOURCE = re.compile(r"\\ProvidesExplPackage|\\RequirePackage\s*(\[[^\]]*\])?\{[^}]*\bexpl3\b|\\ExplSyntaxOn")
_USEPACKAGE = re.compile(r"^([^%\n]*?\\(?:usepackage|RequirePackage)\s*(?:\[[^\]]*\])?\s*)\{([^}]*)\}", re.MULTILINE)


def latexml_binding_dir() -> Path | None:
    """LaTeXML's own Package/ directory (where *.sty.ltxml bindings live)."""
    exe = shutil.which("latexml")
    if exe is None:
        return None
    try:  # Homebrew installs a wrapper script that sets PERL5LIB
        m = re.search(r'PERL5LIB="([^"]+)"', Path(exe).read_text(errors="replace")[:2000])
    except OSError:
        m = None
    libs = m.group(1).split(":") if m else []
    try:
        out = subprocess.run(
            ["perl", *[f"-I{lib}" for lib in libs], "-MLaTeXML::Package", "-e",
             'print $INC{"LaTeXML/Package.pm"}'], capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    d = Path(out).with_suffix("") if out else None  # .../LaTeXML/Package.pm -> .../LaTeXML/Package
    return d if d and d.is_dir() else None


def _has_binding(pkg: str, binding_dirs: list[Path]) -> bool:
    return any((d / f"{pkg}.sty.ltxml").exists() for d in binding_dirs)


def _is_expl3_package(pkg: str) -> bool:
    try:
        path = subprocess.run(["kpsewhich", f"{pkg}.sty"], capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return False
    if not path:
        return False
    return bool(_EXPL3_SOURCE.search(Path(path).read_text(errors="replace")[:20000]))


def drop_expl3_packages(src_dir: Path, stub_dirs: list[Path]) -> list[str]:
    """Remove \\usepackage entries LaTeXML would have to interpret as raw expl3.

    Such packages (fontawesome5, ...) have no LaTeXML binding and are written in
    expl3, which LaTeXML either hangs on or (with our expl3 stub) rejects with a
    flood of errors. Without them, their commands are merely undefined where used.
    Returns the dropped package names.
    """
    builtin = latexml_binding_dir()
    if builtin is None:
        return []
    binding_dirs = [*stub_dirs, builtin]
    dropped: set[str] = set()
    cache: dict[str, bool] = {}

    def keep(pkg: str) -> bool:
        if pkg not in cache:
            cache[pkg] = _has_binding(pkg, binding_dirs) or (src_dir / f"{pkg}.sty").exists() \
                or not _is_expl3_package(pkg)
        if not cache[pkg]:
            dropped.add(pkg)
        return cache[pkg]

    def repl(m: re.Match) -> str:
        pkgs = [p.strip() for p in m.group(2).split(",") if p.strip()]
        kept = [p for p in pkgs if keep(p)]
        if len(kept) == len(pkgs):
            return m.group(0)
        return f"{m.group(1)}{{{','.join(kept)}}}" if kept else ""

    for p in src_dir.rglob("*.tex"):
        text = p.read_text(errors="replace")
        new = _USEPACKAGE.sub(repl, text)
        if new != text:
            p.write_text(new)
    return sorted(dropped)


def _fatal_summary(log_path: Path) -> str:
    lines = log_path.read_text(errors="replace").splitlines()
    fatal = [l for l in lines if l.startswith("Fatal:")]
    return "\n".join(fatal[:3] or lines[-5:])


def run_latexml(main_tex: Path, work: Path, timeout: int = 600) -> LatexmlResult:
    if shutil.which("latexml") is None or shutil.which("latexmlpost") is None:
        raise LatexmlError("LaTeXML is not installed. Run: brew install latexml")
    src_dir = main_tex.parent
    work.mkdir(parents=True, exist_ok=True)
    log_path = work / "latexml.log"
    log_path.write_text("")
    xml = work / "doc.xml"
    html_dir = work / "html"
    if html_dir.exists():
        shutil.rmtree(html_dir)

    stubs = stub_paths(src_dir)
    if STUBS_DIR / "expl3" in stubs:
        dropped = drop_expl3_packages(src_dir, stubs)
        if dropped:
            log.info("not loading expl3-based packages in LaTeXML: %s", ", ".join(dropped))
    cmd = ["latexml", main_tex.name, f"--dest={xml}", "--nocomments", "--noparse"]
    cmd += [f"--path={p}" for p in stubs]
    _run(cmd, src_dir, timeout, log_path)
    if not xml.exists():
        raise LatexmlError(f"LaTeXML could not convert the paper:\n{_fatal_summary(log_path)}")

    n = convert_figures(xml, src_dir)
    log.info("converted %d figures to PNG", n)

    _run(
        ["latexmlpost", str(xml), "--format=html5", "--pmml", "--nomathimages",
         f"--sourcedirectory={src_dir}", f"--dest={html_dir / 'index.html'}",
         "--nodefaultresources", "--timestamp=0"],
        src_dir, timeout, log_path,
    )
    html = html_dir / "index.html"
    if not html.exists():
        raise LatexmlError(f"latexmlpost failed:\n{_fatal_summary(log_path)}")
    errors, warnings = _count(log_path)
    return LatexmlResult(html=html, errors=errors, warnings=warnings, log_path=log_path)
