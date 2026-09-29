"""Turn LaTeXML's single-page HTML into e-reader friendly chapters.

- math -> PNG images (see mathrender), equations -> simple centred blocks
- figures -> copied into the book, oversized ones downscaled
- footnotes -> EPUB3 pop-up footnotes at the end of each chapter
- one chapter per top-level section, with cross-file links rewritten
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup, NavigableString, Tag

from .arxiv import Metadata
from .images import downscale, shrink
from .mathrender import Formula, Rendered, render_formulas

log = logging.getLogger(__name__)


@dataclass
class TocEntry:
    title: str
    href: str
    children: list[TocEntry] = field(default_factory=list)


@dataclass
class Chapter:
    title: str
    filename: str
    body: str  # XHTML fragment
    toc: TocEntry | None = None


@dataclass
class Resource:
    href: str  # path inside the EPUB
    source: Path
    media_type: str


@dataclass
class Book:
    chapters: list[Chapter]
    resources: list[Resource]


_MEDIA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif"}
_REMOVE_SELECTORS = [
    "script", "footer", "nav", ".ltx_page_footer", ".ltx_page_navbar", ".ltx_pagination",
    ".ltx_ERROR", ".ltx_authors", ".ltx_note_type", "link", "style",
]


def _text(el: Tag) -> str:
    return " ".join(el.get_text(" ", strip=True).split())


# ---------------------------------------------------------------- math

def _math_key(m: Tag) -> tuple[str, bool]:
    return (m.get("alttext", "").strip(), m.get("display") == "block")


def _math_node(soup: BeautifulSoup, tex: str, display: bool, r: Rendered | None) -> Tag:
    if r is None:
        code = soup.new_tag("code", attrs={"class": "math-tex"})
        code.string = f"\\[{tex}\\]" if display else f"${tex}$"
        return code
    img = soup.new_tag("img", attrs={"src": f"math/{r.path.name}", "alt": tex})
    if display:
        img["class"] = "math display"
        img["style"] = f"width:{r.width_em:.2f}em"
    else:
        img["class"] = "math inline"
        img["style"] = f"height:{r.height_em:.2f}em;vertical-align:-{r.depth_em:.2f}em"
    return img


def _flatten_equations(soup: BeautifulSoup) -> None:
    """Replace LaTeXML's equation tables with one centred block per row.

    Each row's math cells are merged into a single display formula, so an
    ``align`` line becomes one image and keeps its equation number.
    """
    for table in soup.select("table.ltx_equation, table.ltx_equationgroup"):
        container = soup.new_tag("div", attrs={"class": "equation-group"})
        if table.get("id"):
            container["id"] = table["id"]
        for tr in table.find_all("tr"):
            row_id = tr.get("id") or (tr.parent.get("id") if tr.parent.name == "tbody" else None)
            eqno = tr.select_one(".ltx_eqn_eqno")
            eqno_text = _text(eqno) if eqno else ""
            if eqno:
                eqno.decompose()
            maths = tr.find_all("math")
            div = soup.new_tag("div", attrs={"class": "equation"})
            if row_id and row_id != container.get("id"):
                div["id"] = row_id
            if maths:
                tex = " ".join(m.get("alttext", "") for m in maths)
                new = soup.new_tag("math", attrs={"alttext": tex, "display": "block"})
                div.append(new)
            else:
                text = _text(tr)
                if not text:
                    continue
                div.string = text
            if eqno_text:
                span = soup.new_tag("span", attrs={"class": "eqno"})
                span.string = eqno_text
                div.append(span)
            container.append(div)
        table.replace_with(container)


def _render_math(soup: BeautifulSoup, main_tex: Path, out_dir: Path,
                 aux: Path | None) -> list[Resource]:
    maths = soup.find_all("math")
    keys: dict[tuple[str, bool], int] = {}
    for m in maths:
        keys.setdefault(_math_key(m), len(keys))
    formulas = [Formula(tex, display) for (tex, display) in keys]
    rendered = render_formulas(formulas, main_tex, out_dir / "math", aux=aux)
    for m in maths:
        key = _math_key(m)
        m.replace_with(_math_node(soup, key[0], key[1], rendered[keys[key]]))
    return [
        Resource(f"math/{r.path.name}", r.path, "image/png") for r in rendered if r is not None
    ]


# ---------------------------------------------------------------- figures

def _collect_images(soup: BeautifulSoup, html_dir: Path, out_dir: Path) -> list[Resource]:
    resources: dict[str, Resource] = {}
    for img in soup.find_all("img"):
        if "math" in (img.get("class") or []):
            continue
        src = img.get("src", "")
        path = (html_dir / src).resolve() if src and not src.startswith("data:") else None
        if path is None or not path.is_file() or path.suffix.lower() not in _MEDIA:
            log.warning("dropping missing/unsupported image %r", src[:80])
            img.decompose()
            continue
        key = "images/" + re.sub(r"[^A-Za-z0-9._-]", "_", src)
        if key not in resources:
            dest = out_dir / key
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
            downscale(dest)
            final = shrink(dest)
            href = str(final.relative_to(out_dir))
            resources[key] = Resource(href, final, _MEDIA[final.suffix.lower()])
        img["src"] = resources[key].href
        for attr in ("width", "height", "title"):
            img.attrs.pop(attr, None)
        if img.get("alt") in (None, "", "Refer to caption"):
            img["alt"] = "Figure"
    return list(resources.values())


# ---------------------------------------------------------------- footnotes

def _extract_footnotes(soup: BeautifulSoup, root: Tag) -> list[Tag]:
    """Replace footnotes under ``root`` with noterefs; return the <aside> notes."""
    notes = []
    for note in root.select(".ltx_note"):
        if "ltx_role_footnotemark" in note.get("class", []):
            mark = note.select_one(".ltx_note_mark")
            note.replace_with(soup.new_tag("sup") if mark is None else _sup(soup, _text(mark)))
            continue
        content = note.select_one(".ltx_note_content")
        mark_el = note.select_one(".ltx_note_mark")
        mark = _text(mark_el) if mark_el else "*"
        n = note.get("id") or f"note{len(notes) + 1}"
        if content is None:
            note.decompose()
            continue
        for junk in content.select(".ltx_note_mark, .ltx_tag_note"):
            junk.decompose()
        ref = soup.new_tag("a", attrs={"href": f"#fn-{n}", "id": f"fnref-{n}", "epub:type": "noteref", "class": "noteref"})
        ref.append(_sup(soup, mark))
        note.replace_with(ref)
        aside = soup.new_tag("aside", attrs={"epub:type": "footnote", "id": f"fn-{n}", "class": "footnote"})
        p = soup.new_tag("p")
        back = soup.new_tag("a", attrs={"href": f"#fnref-{n}"})
        back.string = mark
        p.append(back)
        p.append(" ")
        for child in list(content.contents):
            p.append(child.extract())
        aside.append(p)
        notes.append(aside)
    return notes


def _sup(soup: BeautifulSoup, text: str) -> Tag:
    s = soup.new_tag("sup")
    s.string = text
    return s


# ---------------------------------------------------------------- splitting

def _section_title(section: Tag) -> str:
    h = section.find(re.compile(r"^h[1-6]$"))
    return _text(h) if h else "Section"


def _toc_for(section: Tag, filename: str, fragment: str | None = None) -> TocEntry:
    entry = TocEntry(_section_title(section), f"{filename}#{fragment}" if fragment else filename)
    for sub in section.find_all("section", recursive=False):
        if sub.get("id") and "ltx_paragraph" not in sub.get("class", []):
            entry.children.append(_toc_for(sub, filename, sub["id"]))
    return entry


def _front_matter(soup: BeautifulSoup, meta: Metadata, nodes: list[Tag]) -> Tag:
    div = soup.new_tag("div", attrs={"class": "front-matter"})
    h1 = soup.new_tag("h1", attrs={"class": "title"})
    h1.string = meta.title
    div.append(h1)
    if meta.authors:
        p = soup.new_tag("p", attrs={"class": "authors"})
        p.string = ", ".join(meta.authors)
        div.append(p)
    p = soup.new_tag("p", attrs={"class": "arxiv-id"})
    p.string = f"arXiv:{meta.arxiv_id}" + (f" · {meta.published}" if meta.published else "")
    div.append(p)
    for n in nodes:
        if n.name == "h1" and "ltx_title_document" in n.get("class", []):
            continue  # replaced by metadata title
        div.append(n)
    return div


def build_book(html_path: Path, meta: Metadata, main_tex: Path, out_dir: Path,
               aux: Path | None = None) -> Book:
    """Post-process LaTeXML HTML into chapters + resources staged under ``out_dir``."""
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "lxml")

    for sel in _REMOVE_SELECTORS:
        for el in soup.select(sel):
            el.decompose()

    # Colours are meaningless (or unreadable) on e-ink.
    for el in soup.find_all(style=True):
        style = re.sub(r"(?<![-\w])(background-)?color\s*:[^;]*;?", "", el["style"]).strip()
        if style:
            el["style"] = style
        else:
            del el["style"]

    # \resizebox etc. become spans with CSS transforms around tables; transforms and
    # fixed sizes are meaningless on e-readers, and blocks inside spans are invalid.
    for el in soup.select(".ltx_transformed_outer, .ltx_transformed_inner"):
        el.name = "div"
        el.attrs.pop("style", None)
    for el in soup.find_all("span"):
        if el.find(["table", "div", "p", "ul", "ol", "figure", "section"]):
            el.name = "div"

    # LaTeXML emits <figure>s with several <figcaption>s (side-by-side subtables),
    # which isn't valid XHTML; plain divs with the same classes are valid anywhere.
    for tag, cls in (("figure", "figure"), ("figcaption", "caption")):
        for el in soup.find_all(tag):
            el.name = "div"
            el["class"] = [cls, *el.get("class", [])]

    article = soup.find("article") or soup.body
    _flatten_equations(soup)
    resources = _render_math(soup, main_tex, out_dir, aux)
    resources += _collect_images(soup, html_path.parent, out_dir)

    # Group top-level nodes: everything before the first section is front matter.
    front: list[Tag] = []
    groups: list[list[Tag]] = []
    for node in list(article.children):
        if isinstance(node, NavigableString):
            continue
        node = node.extract()
        if node.name == "section":
            groups.append([node])
        elif groups:
            groups[-1].append(node)
        else:
            front.append(node)

    chapters_nodes: list[tuple[str, str, Tag, TocEntry]] = []
    fm = _front_matter(soup, meta, front)
    chapters_nodes.append(("Title & Abstract", "front.xhtml", fm, TocEntry("Title & Abstract", "front.xhtml")))
    for i, group in enumerate(groups, 1):
        filename = f"ch{i:02d}.xhtml"
        wrapper = soup.new_tag("div", attrs={"class": "chapter"})
        for n in group:
            wrapper.append(n)
        title = _section_title(group[0])
        chapters_nodes.append((title, filename, wrapper, _toc_for(group[0], filename)))

    # Map every id to its chapter file, then rewrite in-document links.
    id_file = {}
    for _, filename, node, _ in chapters_nodes:
        for el in node.find_all(id=True):
            id_file[el["id"]] = filename
        if node.get("id"):
            id_file[node["id"]] = filename

    chapters = []
    for title, filename, node, toc in chapters_nodes:
        notes = _extract_footnotes(soup, node)
        if notes:
            section = soup.new_tag("section", attrs={"class": "footnotes"})
            for aside in notes:
                section.append(aside)
            node.append(section)
        local_ids = {el["id"] for el in [node, *node.find_all(id=True)] if el.get("id")}
        for a in node.find_all("a", href=True):
            href = a["href"]
            if href.startswith("#"):
                target = id_file.get(href[1:])
                if target and target != filename:
                    a["href"] = f"{target}{href}"
                elif target is None and href[1:] not in local_ids:
                    a.unwrap()  # dangling reference (e.g. a dropped bib entry)
                    continue
            if a.get("title") == "":
                del a["title"]
        chapters.append(Chapter(title=title, filename=filename, body=str(node), toc=toc))

    return Book(chapters=chapters, resources=resources)


