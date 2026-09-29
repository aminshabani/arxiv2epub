"""Figure rasterization and image resizing."""

from __future__ import annotations

import io
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf
from PIL import Image

FIGURE_DPI = 200
MAX_WIDTH = 1600

_LEN = re.compile(r"(-?[\d.]+)\s*pt")


def _parse_box(options: str, key: str) -> list[float] | None:
    m = re.search(rf"(?:^|,){key}=([^,]+)", options)
    if not m:
        return None
    vals = [float(v) for v in _LEN.findall(m.group(1))]
    return vals if len(vals) == 4 else None


def rasterize_figure(src: Path, dest: Path, options: str = "") -> Path:
    """Render the first page of a PDF/EPS/SVG to PNG, honouring trim/viewport.

    Returns the written path, which may be a .jpg if that is much smaller.
    """
    suffix = src.suffix.lower()
    if suffix in (".eps", ".ps"):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "fig.pdf"
            _eps_to_pdf(src, pdf)
            return rasterize_figure(pdf, dest, options)

    doc = pymupdf.open(src)
    page = doc[0]
    rect = page.rect
    clip = None
    # graphicx boxes are in bottom-left-origin pt; PyMuPDF uses top-left.
    if (vp := _parse_box(options, "viewport")) is not None:
        llx, lly, urx, ury = vp
        clip = pymupdf.Rect(llx, rect.height - ury, urx, rect.height - lly)
    elif (tr := _parse_box(options, "trim")) is not None:
        left, bottom, right, top = tr
        clip = pymupdf.Rect(left, top, rect.width - right, rect.height - bottom)
    if clip is not None:
        clip = clip & rect
        if clip.is_empty:
            clip = None
    pix = page.get_pixmap(dpi=FIGURE_DPI, clip=clip, alpha=False)
    dest.parent.mkdir(parents=True, exist_ok=True)
    pix.save(dest)
    doc.close()
    downscale(dest)
    return shrink(dest)


def _eps_to_pdf(src: Path, dest: Path) -> None:
    gs = shutil.which("gs")
    if gs is None:
        raise RuntimeError("Ghostscript (gs) is needed to convert EPS figures")
    subprocess.run(
        [gs, "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-dEPSCrop",
         "-sDEVICE=pdfwrite", f"-sOutputFile={dest}", str(src)],
        check=True, capture_output=True, timeout=120,
    )


def downscale(path: Path, max_width: int = MAX_WIDTH) -> None:
    """Shrink an image in place if it is wider than ``max_width``."""
    with Image.open(path) as im:
        if im.width <= max_width:
            return
        h = round(im.height * max_width / im.width)
        fmt = im.format
        small = im.resize((max_width, h), Image.LANCZOS)
    small.save(path, format=fmt)


JPEG_THRESHOLD = 300_000


def shrink(path: Path) -> Path:
    """Re-encode a large PNG as JPEG if that at least halves it (photos, sample grids).

    Returns the path of the file to use (the original, or a new .jpg beside it).
    """
    if path.suffix.lower() != ".png" or path.stat().st_size < JPEG_THRESHOLD:
        return path
    with Image.open(path) as im:
        if im.mode in ("RGBA", "LA", "P"):
            if im.mode == "P":
                im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
    if buf.tell() * 2 > path.stat().st_size:
        return path
    jpg = path.with_suffix(".jpg")
    jpg.write_bytes(buf.getvalue())
    return jpg
