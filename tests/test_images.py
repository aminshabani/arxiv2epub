import pymupdf
import pytest
from PIL import Image

from arxiv2epub import images
from arxiv2epub.images import MAX_PIXELS, MAX_WIDTH, ImageTooLarge, downscale, rasterize_figure


def test_safe_dpi():
    a4 = pymupdf.Rect(0, 0, 595, 842)
    assert images.safe_dpi(a4, 200) == 200  # normal pages are untouched
    huge = pymupdf.Rect(0, 0, 7200, 7200)  # 100 x 100 inches
    dpi = images.safe_dpi(huge, 200)
    assert (100 * dpi) ** 2 <= MAX_PIXELS
    assert images.safe_dpi(huge, 200, max_width=1600) == 32


def test_rasterize_huge_page(tmp_path):
    pdf = tmp_path / "big.pdf"
    doc = pymupdf.open()
    doc.new_page(width=7200, height=7200).draw_rect(pymupdf.Rect(100, 100, 7000, 7000), fill=(1, 0, 0))
    doc.save(pdf)
    out = rasterize_figure(pdf, tmp_path / "big.png")
    with Image.open(out) as im:
        assert im.width <= MAX_WIDTH and im.width * im.height <= MAX_PIXELS


def test_downscale_large_jpeg_uses_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "MAX_PIXELS", 1_000_000)
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (4000, 2000), "gray").save(path)  # 8 MP, over the patched limit
    downscale(path, max_width=800)  # decoded at 1/4 scale, so it's allowed
    with Image.open(path) as im:
        assert im.size == (800, 400) and im.format == "JPEG"


def test_downscale_refuses_huge_png(tmp_path, monkeypatch):
    monkeypatch.setattr(images, "MAX_PIXELS", 1_000_000)
    path = tmp_path / "bomb.png"
    Image.new("L", (2000, 2000)).save(path)
    with pytest.raises(ImageTooLarge):
        downscale(path)


def test_downscale_small_image_untouched(tmp_path):
    path = tmp_path / "small.png"
    Image.new("RGB", (300, 200), "white").save(path)
    before = path.read_bytes()
    downscale(path)
    assert path.read_bytes() == before


def test_rasterize_honours_trim(tmp_path):
    pdf = tmp_path / "fig.pdf"
    doc = pymupdf.open()
    doc.new_page(width=200, height=100)
    doc.save(pdf)
    out = rasterize_figure(pdf, tmp_path / "fig.png", "width=5cm,trim=0pt 0pt 100pt 0pt,clip")
    with Image.open(out) as im:
        assert im.size == (round(100 * 200 / 72), round(100 * 200 / 72))
