"""Command-line interface: ``arxiv2epub <id>`` and ``arxiv2epub doctor``."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import arxiv, sandbox
from .epub import write_epub
from .latexml import LatexmlError, run_latexml
from .postprocess import build_book
from .prerender import prerender
from .source import SourceError, check_convertible, extract, find_main_tex
from .texsnippets import build_aux

log = logging.getLogger("arxiv2epub")

TOOLS = [
    ("docker", "brew install --cask docker (runs conversions in a sandbox)", True),
    # The rest ship in the sandbox image; on the host they're only used with --no-sandbox.
    ("latexml", "brew install latexml (only for --no-sandbox)", False),
    ("latexmlpost", "brew install latexml (only for --no-sandbox)", False),
    ("pdflatex", "brew install --cask mactex-no-gui (only for --no-sandbox)", False),
    ("xelatex", "install MacTeX (only for --no-sandbox, papers using fontspec)", False),
    ("gs", "brew install ghostscript (only for --no-sandbox, EPS figures)", False),
    ("epubcheck", "brew install epubcheck (optional, for validating output)", False),
]


def doctor() -> int:
    ok = True
    for tool, hint, required in TOOLS:
        path = shutil.which(tool)
        status = "ok" if path else ("MISSING" if required else "missing (optional)")
        print(f"  {tool:<12} {status:<20} {path or hint}")
        ok &= bool(path) or not required
    if shutil.which("docker"):
        try:
            sandbox.check_available()
            print(f"  {'':<12} {'daemon running':<20} sandbox image: {sandbox.image_tag()}")
        except sandbox.SandboxError as e:
            print(f"  {'':<12} {'NOT RUNNING':<20} {e}")
            ok = False
    print("\nAll required tools found." if ok else "\nSome required tools are missing.")
    return 0 if ok else 1


def convert(args: argparse.Namespace) -> int:
    t0 = time.monotonic()
    arxiv_id = arxiv.parse_id(args.paper)
    name = arxiv.safe_name(arxiv_id)
    out = Path(args.output).expanduser() / f"{name}.epub"
    if args.sandbox:
        sandbox.check_available()  # fail before downloading anything

    print(f"[1/6] Fetching metadata for {arxiv_id}")
    meta = arxiv.fetch_metadata(arxiv_id)
    print(f"      {meta.title}")

    print("[2/6] Downloading source")
    bundle = arxiv.download_source(arxiv_id, use_cache=not args.no_cache)
    log_dest = arxiv.CACHE_DIR / name / "latexml.log"
    kept = arxiv.CACHE_DIR / name / "work" if args.keep_work else None

    if args.sandbox:
        meta_json = bundle.with_name("meta.json")
        meta_json.write_text(json.dumps(dataclasses.asdict(meta)))
        sandbox.run(bundle, meta_json, out, timeout=args.timeout, memory=args.sandbox_memory,
                    work_dir=kept, log_dest=log_dest, rebuild=args.rebuild_image,
                    verbose=args.verbose)
        if kept:
            print(f"      work files kept in {kept}")
    else:
        print("      warning: running without the sandbox; the paper's LaTeX can read your files")
        work_ctx = tempfile.TemporaryDirectory(prefix="arxiv2epub-") if kept is None else None
        work = Path(work_ctx.name) if work_ctx else kept
        try:
            build(bundle, meta, out, work, timeout=args.timeout, log_dest=log_dest)
        except LatexmlError:
            print(f"      full LaTeXML log saved to {log_dest}")
            raise
        finally:
            if work_ctx:
                work_ctx.cleanup()
            else:
                print(f"      work files kept in {work}")

    size = out.stat().st_size / 1e6
    print(f"\nDone in {time.monotonic() - t0:.0f}s: {out} ({size:.1f} MB)")
    return 0


def build(bundle: Path, meta: arxiv.Metadata, out: Path, work: Path, timeout: int,
          log_dest: Path) -> None:
    """Steps 2-6 on an already downloaded bundle. This is the part the sandbox runs."""
    src = extract(bundle, work / "src")
    main_tex = find_main_tex(src)
    check_convertible(main_tex)
    print(f"      main file: {main_tex.relative_to(src)}")

    print("[3/6] Typesetting with LaTeX (pictures, wide tables)")
    aux = build_aux(main_tex, work / "auxbuild", timeout=timeout)
    counts = prerender(main_tex, work / "prebuild", aux=aux, timeout=timeout)
    print(f"      {counts['pictures']} pictures, {counts['tables']} wide tables as images")

    print("[4/6] Converting with LaTeXML (this can take a minute)")
    try:
        result = run_latexml(main_tex, work / "latexml", timeout=timeout)
    except LatexmlError:
        log_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(work / "latexml" / "latexml.log", log_dest)
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


def _build_in_sandbox(argv: list[str]) -> int:
    """Entry point inside the container: ``_build <source> <meta.json> --work DIR``.

    Progress goes to stderr. stdout carries the result, which is the EPUB on
    success or LaTeXML's log if LaTeXML fails, because the container can't
    write any host files.
    """
    p = argparse.ArgumentParser(prog="arxiv2epub _build")
    p.add_argument("bundle", type=Path)
    p.add_argument("meta", type=Path)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)
    # Keep the real stdout for the result and point fd 1 at stderr, so nothing
    # else (Python prints, child processes, C libraries) can write into the EPUB.
    sys.stdout.flush()
    result = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    meta = arxiv.Metadata(**json.loads(args.meta.read_text()))
    out, log_dest = args.work / "book.epub", args.work / "latexml.log"
    try:
        build(args.bundle, meta, out, args.work, timeout=args.timeout, log_dest=log_dest)
    except LatexmlError:
        result.write(log_dest.read_bytes())
        raise
    finally:
        result.flush()
    result.write(out.read_bytes())
    result.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["doctor"]:
        return doctor()
    if argv[:1] == ["_build"]:
        logging.basicConfig(
            level=logging.DEBUG if "--verbose" in argv else logging.WARNING,
            format="      %(levelname)s %(name)s: %(message)s",
        )
        try:
            return _build_in_sandbox(argv[1:])
        except (SourceError, LatexmlError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130

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
    p.add_argument("--no-sandbox", dest="sandbox", action="store_false",
                   help="run LaTeX and the converters directly on this machine instead of in Docker")
    p.add_argument("--sandbox-memory", default=sandbox.DEFAULT_MEMORY,
                   help=f"memory limit for the sandbox (default: {sandbox.DEFAULT_MEMORY})")
    p.add_argument("--rebuild-image", action="store_true", help="rebuild the sandbox image")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="      %(levelname)s %(name)s: %(message)s",
    )
    try:
        return convert(args)
    except (arxiv.ArxivError, SourceError, LatexmlError, sandbox.SandboxError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
