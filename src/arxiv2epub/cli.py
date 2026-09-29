"""Command-line interface: ``arxiv2epub <id>`` and ``arxiv2epub doctor``."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import arxiv
from .epub import write_epub
from .latexml import LatexmlError, run_latexml
from .postprocess import build_book
from .prerender import prerender
from .source import SourceError, check_convertible, extract, find_main_tex
from .texsnippets import build_aux

log = logging.getLogger("arxiv2epub")

TOOLS = [
    ("latexml", "brew install latexml", True),
    ("latexmlpost", "brew install latexml", True),
    ("pdflatex", "install MacTeX: brew install --cask mactex-no-gui", True),
    ("xelatex", "install MacTeX (only needed for papers using fontspec)", False),
    ("gs", "brew install ghostscript (only needed for EPS figures)", False),
    ("epubcheck", "brew install epubcheck (optional, for validating output)", False),
]


def doctor() -> int:
    ok = True
    for tool, hint, required in TOOLS:
        path = shutil.which(tool)
        status = "ok" if path else ("MISSING" if required else "missing (optional)")
        print(f"  {tool:<12} {status:<20} {path or hint}")
        ok &= bool(path) or not required
    print("\nAll required tools found." if ok else "\nSome required tools are missing.")
    return 0 if ok else 1


def convert(args: argparse.Namespace) -> int:
    t0 = time.monotonic()
    arxiv_id = arxiv.parse_id(args.paper)
    name = arxiv.safe_name(arxiv_id)
    out = Path(args.output).expanduser() / f"{name}.epub"

    print(f"[1/6] Fetching metadata for {arxiv_id}")
    meta = arxiv.fetch_metadata(arxiv_id)
    print(f"      {meta.title}")

    print("[2/6] Downloading source")
    bundle = arxiv.download_source(arxiv_id, use_cache=not args.no_cache)

    work_ctx = (
        tempfile.TemporaryDirectory(prefix="arxiv2epub-") if not args.keep_work else None
    )
    work = Path(work_ctx.name) if work_ctx else arxiv.CACHE_DIR / name / "work"
    try:
        src = extract(bundle, work / "src")
        main_tex = find_main_tex(src)
        check_convertible(main_tex)
        print(f"      main file: {main_tex.relative_to(src)}")

        print("[3/6] Typesetting with LaTeX (pictures, wide tables)")
        aux = build_aux(main_tex, work / "auxbuild", timeout=args.timeout)
        counts = prerender(main_tex, work / "prebuild", aux=aux, timeout=args.timeout)
        print(f"      {counts['pictures']} pictures, {counts['tables']} wide tables as images")

        print("[4/6] Converting with LaTeXML (this can take a minute)")
        try:
            result = run_latexml(main_tex, work / "latexml", timeout=args.timeout)
        except LatexmlError:
            saved = arxiv.CACHE_DIR / name / "latexml.log"
            shutil.copyfile(work / "latexml" / "latexml.log", saved)
            print(f"      full LaTeXML log saved to {saved}")
            raise
        if result.errors:
            print(f"      LaTeXML reported {result.errors} errors (usually harmless; "
                  f"run with --keep-work to inspect the log)")

        print("[5/6] Rendering math and figures")
        book = build_book(result.html, meta, main_tex, work / "stage", aux=aux)
        n_math = sum(r.href.startswith("math/") for r in book.resources)
        print(f"      {len(book.chapters)} chapters, {n_math} formula images, "
              f"{len(book.resources) - n_math} figures")

        print("[6/6] Writing EPUB")
        write_epub(book, meta, work / "stage", out)
    finally:
        if work_ctx:
            work_ctx.cleanup()
        else:
            print(f"      work files kept in {work}")

    size = out.stat().st_size / 1e6
    print(f"\nDone in {time.monotonic() - t0:.0f}s: {out} ({size:.1f} MB)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["doctor"]:
        return doctor()

    p = argparse.ArgumentParser(
        prog="arxiv2epub",
        description="Convert an arXiv paper to a Kindle-friendly EPUB. "
        "Run `arxiv2epub doctor` to check dependencies.",
    )
    p.add_argument("paper", help="arXiv ID or URL, e.g. 1706.03762 or https://arxiv.org/abs/1706.03762")
    p.add_argument("-o", "--output", default=".", help="output directory (default: current)")
    p.add_argument("--timeout", type=int, default=600, help="LaTeXML timeout in seconds")
    p.add_argument("--no-cache", action="store_true", help="re-download the source")
    p.add_argument("--keep-work", action="store_true", help="keep intermediate files for debugging")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="      %(levelname)s %(name)s: %(message)s",
    )
    try:
        return convert(args)
    except (arxiv.ArxivError, SourceError, LatexmlError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
