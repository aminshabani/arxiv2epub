"""Render TeX formulas to PNG images sized in ``em`` for inline use."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .images import safe_dpi
from .texsnippets import BORDER_PT, compile_snippets

log = logging.getLogger(__name__)

RENDER_DPI = 300

_STRIP = re.compile(r"\\(label|tag\*?)\s*\{[^{}]*\}|\\(nonumber|notag)\b")


@dataclass
class Formula:
    tex: str
    display: bool


@dataclass
class Rendered:
    path: Path
    width_em: float
    height_em: float
    depth_em: float  # distance from baseline to image bottom


def formula_body(f: Formula) -> str:
    tex = _STRIP.sub("", f.tex).strip()
    # split only works directly inside a display environment; aligned is equivalent here.
    tex = re.sub(r"\\(begin|end)\{split\}", r"\\\1{aligned}", tex)
    if ("&" in tex.replace(r"\&", "") or r"\\" in tex) and r"\begin" not in tex:
        tex = r"\begin{aligned}" + tex + r"\end{aligned}"
    return f"$\\displaystyle {tex}$" if f.display else f"${tex}$"


def render_formulas(
    formulas: list[Formula], main_tex: Path, out_dir: Path,
    aux: Path | None = None, timeout: int = 600,
) -> list[Rendered | None]:
    """Render formulas to ``out_dir/mN.png``. Entries that failed are None."""
    out_dir.mkdir(parents=True, exist_ok=True)
    placed = compile_snippets(
        [formula_body(f) for f in formulas], main_tex, out_dir.parent / "mathbuild",
        job="a2e_math", aux=aux, timeout=timeout,
    )
    results: list[Rendered | None] = []
    docs: dict[Path, pymupdf.Document] = {}
    try:
        for i, p in enumerate(placed):
            if p is None:
                results.append(None)
                continue
            doc = docs.setdefault(p.pdf, pymupdf.open(p.pdf))
            page = doc[p.page]
            path = out_dir / f"m{i}.png"
            dpi = safe_dpi(page.rect, RENDER_DPI)  # only binds for runaway snippets
            page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False).save(path)
            results.append(Rendered(
                path=path,
                width_em=page.rect.width / p.fontsize,
                height_em=page.rect.height / p.fontsize,
                depth_em=(p.depth + BORDER_PT) / p.fontsize,
            ))
    finally:
        for d in docs.values():
            d.close()
    failed = results.count(None)
    if failed:
        log.warning("%d formulas could not be rendered; showing their TeX source", failed)
    return results
