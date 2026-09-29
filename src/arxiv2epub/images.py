"""Figure rasterization and image resizing."""

from __future__ import annotations

import io
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf
from PIL import Image

FIGURE_DPI = 200
MAX_WIDTH = 1600
# Hard ceiling on pixels we render or decode (~120 MB as RGB). Figures in the
# source are untrusted: a 13000x13000 PNG or a PDF page several metres wide
# would otherwise be decoded in full before being shrunk.
MAX_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ImageTooLarge(Exception):
    pass


def safe_dpi(rect: pymupdf.Rect, dpi: float, max_width: int | None = None) -> float:
    """Lower ``dpi`` so rendering ``rect`` (in pt) stays within ``MAX_PIXELS``.

    With ``max_width``, also avoid rendering wider than the image will end up.
    """
    w_in, h_in = rect.width / 72, rect.height / 72
    if w_in <= 0 or h_in <= 0:
        return dpi
    dpi = min(dpi, math.sqrt(MAX_PIXELS / (w_in * h_in)))
    if max_width:
        # Render at 2x the final width at most so the downscale stays sharp.
        dpi = min(dpi, 2 * max_width / w_in)
    return dpi


def _open(path: Path) -> Image.Image:
    """Open an image lazily (header only); callers check its size before decoding.

    Pillow's own bomb check runs at open time and would also reject big JPEGs,
    which :func:`downscale` can shrink cheaply, so it is lifted here.
    """
    limit, Image.MAX_IMAGE_PIXELS = Image.MAX_IMAGE_PIXELS, None
    try:
        return Image.open(path)
    finally:
        Image.MAX_IMAGE_PIXELS = limit


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
    dpi = safe_dpi(clip or rect, FIGURE_DPI, MAX_WIDTH)
    pix = page.get_pixmap(dpi=dpi, clip=clip, alpha=False)
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
    """Shrink an image in place if it is wider than ``max_width``.

    JPEGs are decoded at reduced scale, so even huge photos are cheap. Anything
    still above ``MAX_PIXELS`` raises :class:`ImageTooLarge` without decoding.
    """
    with _open(path) as im:
        w, h = im.size
        fmt = im.format
        target = (max_width, round(h * max_width / w)) if w > max_width else im.size
        if fmt == "JPEG":
            im.draft(im.mode, target)  # libjpeg decodes at 1/2, 1/4 or 1/8 scale
        if im.width * im.height > MAX_PIXELS:
            raise ImageTooLarge(f"{w}x{h} pixels")
        if w <= max_width:
            return
        im.thumbnail(target, Image.LANCZOS)
        small = im.copy()
    small.save(path, format=fmt)


JPEG_THRESHOLD = 300_000


def shrink(path: Path) -> Path:
    """Re-encode a large PNG as JPEG if that at least halves it (photos, sample grids).

    Returns the path of the file to use (the original, or a new .jpg beside it).
    """
    if path.suffix.lower() != ".png" or path.stat().st_size < JPEG_THRESHOLD:
        return path
    with _open(path) as im:
        if im.width * im.height > MAX_PIXELS:
            raise ImageTooLarge(f"{im.width}x{im.height} pixels")
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
