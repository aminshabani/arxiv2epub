"""Write an EPUB3 (with EPUB2 NCX fallback) from post-processed chapters."""

from __future__ import annotations

import datetime as dt
import textwrap
import uuid
import zipfile
from html import escape
from importlib import resources as pkg_resources
from pathlib import Path

import lxml.html
from lxml import etree
from PIL import Image, ImageDraw, ImageFont

from .arxiv import Metadata
from .postprocess import Book, TocEntry

EPUB_TYPE_PLACEHOLDER = "data-epubtype"


def to_xhtml(fragment: str) -> str:
    """Serialize an HTML fragment as well-formed XHTML."""
    fragment = fragment.replace('epub:type="', f'{EPUB_TYPE_PLACEHOLDER}="')
    el = lxml.html.fragment_fromstring(fragment, create_parent="div")
    out = "".join(
        etree.tostring(child, method="xml", encoding="unicode", with_tail=True)
        for child in el
    )
    if el.text:
        out = escape(el.text, quote=False) + out
    return out.replace(f'{EPUB_TYPE_PLACEHOLDER}="', 'epub:type="')


def _page(title: str, body: str, css: bool = True) -> str:
    link = '<link rel="stylesheet" type="text/css" href="style.css"/>' if css else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" '
        'lang="en" xml:lang="en">\n'
        f"<head><meta charset=\"utf-8\"/><title>{escape(title)}</title>{link}</head>\n"
        f"<body>\n{body}\n</body>\n</html>\n"
    )


def _nav_list(entries: list[TocEntry]) -> str:
    items = []
    for e in entries:
        sub = f"\n{_nav_list(e.children)}" if e.children else ""
        items.append(f'<li><a href="{escape(e.href)}">{escape(e.title)}</a>{sub}</li>')
    return "<ol>" + "".join(items) + "</ol>"


def _ncx_points(entries: list[TocEntry], counter: list[int]) -> str:
    out = []
    for e in entries:
        counter[0] += 1
        n = counter[0]
        out.append(
            f'<navPoint id="np{n}" playOrder="{n}"><navLabel><text>{escape(e.title)}</text></navLabel>'
            f'<content src="{escape(e.href)}"/>{_ncx_points(e.children, counter)}</navPoint>'
        )
    return "".join(out)


_FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Georgia.ttf",
    "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/Library/Fonts/Georgia.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
]


def _font(size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def make_cover(meta: Metadata, dest: Path) -> None:
    W, H = 1600, 2560
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    d.rectangle([60, 60, W - 60, H - 60], outline=0, width=6)
    y = 380
    title_font = _font(110)
    for line in textwrap.wrap(meta.title, 22)[:8]:
        w = d.textlength(line, font=title_font)
        d.text(((W - w) / 2, y), line, font=title_font, fill=0)
        y += 140
    y += 120
    d.line([W / 2 - 200, y, W / 2 + 200, y], fill=0, width=4)
    y += 120
    author_font = _font(60)
    authors = meta.authors if len(meta.authors) <= 6 else meta.authors[:5] + ["et al."]
    for line in textwrap.wrap(", ".join(authors), 42)[:8]:
        w = d.textlength(line, font=author_font)
        d.text(((W - w) / 2, y), line, font=author_font, fill=0)
        y += 85
    id_font = _font(64)
    label = f"arXiv:{meta.arxiv_id}"
    w = d.textlength(label, font=id_font)
    d.text(((W - w) / 2, H - 300), label, font=id_font, fill=0)
    im.save(dest, "JPEG", quality=85)


def write_epub(book: Book, meta: Metadata, stage_dir: Path, dest: Path) -> Path:
    css = pkg_resources.files("arxiv2epub").joinpath("assets/kindle.css").read_text()
    cover = stage_dir / "cover.jpg"
    make_cover(meta, cover)

    book_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'https://arxiv.org/abs/{meta.arxiv_id}')}"
    modified = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    manifest = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
        '<item id="cover-img" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>',
        '<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>',
    ]
    spine = ['<itemref idref="cover" linear="yes"/>']
    for i, ch in enumerate(book.chapters):
        manifest.append(f'<item id="c{i}" href="{ch.filename}" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{i}"/>')
    for i, r in enumerate(book.resources):
        manifest.append(f'<item id="r{i}" href="{escape(r.href)}" media-type="{r.media_type}"/>')

    creators = "".join(
        f'<dc:creator id="a{i}">{escape(a)}</dc:creator>' for i, a in enumerate(meta.authors)
    )
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="en">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="bookid">{book_id}</dc:identifier>
<dc:title>{escape(meta.title)}</dc:title>
{creators}
<dc:language>en</dc:language>
<dc:source>https://arxiv.org/abs/{escape(meta.arxiv_id)}</dc:source>
<dc:publisher>arXiv</dc:publisher>
{f"<dc:date>{meta.published}</dc:date>" if meta.published else ""}
<dc:description>{escape(meta.abstract)}</dc:description>
<meta property="dcterms:modified">{modified}</meta>
<meta name="cover" content="cover-img"/>
</metadata>
<manifest>
{chr(10).join(manifest)}
</manifest>
<spine toc="ncx">
{chr(10).join(spine)}
</spine>
<guide><reference type="cover" title="Cover" href="cover.xhtml"/>
<reference type="text" title="Start" href="{book.chapters[0].filename}"/></guide>
</package>
"""
    toc_entries = [c.toc for c in book.chapters if c.toc]
    nav = _page(
        "Contents",
        f'<nav epub:type="toc" id="toc"><h1>Contents</h1>{_nav_list(toc_entries)}</nav>\n'
        '<nav epub:type="landmarks" hidden=""><ol>'
        '<li><a epub:type="cover" href="cover.xhtml">Cover</a></li>'
        f'<li><a epub:type="bodymatter" href="{book.chapters[0].filename}">Start</a></li>'
        "</ol></nav>",
    )
    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="{book_id}"/></head>
<docTitle><text>{escape(meta.title)}</text></docTitle>
<navMap>{_ncx_points(toc_entries, [0])}</navMap>
</ncx>
"""
    cover_page = _page(
        "Cover",
        '<div class="cover"><img src="cover.jpg" alt="Cover"/></div>',
    )

    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>\n<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
            compress_type=zipfile.ZIP_DEFLATED,
        )

        def put(name: str, data: str | bytes) -> None:
            z.writestr(f"OEBPS/{name}", data, compress_type=zipfile.ZIP_DEFLATED)

        put("content.opf", opf)
        put("nav.xhtml", nav)
        put("toc.ncx", ncx)
        put("style.css", css)
        put("cover.xhtml", cover_page)
        z.write(cover, "OEBPS/cover.jpg", compress_type=zipfile.ZIP_STORED)
        for ch in book.chapters:
            put(ch.filename, _page(ch.title, to_xhtml(ch.body)))
        for r in book.resources:
            z.write(r.source, f"OEBPS/{r.href}", compress_type=zipfile.ZIP_STORED)
    return dest
