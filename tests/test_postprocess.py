import zipfile

from arxiv2epub.arxiv import Metadata
from arxiv2epub.epub import to_xhtml, write_epub
from arxiv2epub.postprocess import build_book

HTML = """<!DOCTYPE html><html><body><div class="ltx_page_main"><div class="ltx_page_content">
<article class="ltx_document">
<h1 class="ltx_title ltx_title_document">Paper</h1>
<div class="ltx_authors">messy authors</div>
<div class="ltx_abstract"><h6>Abstract</h6><p>We study <span style="color:#FF0000;font-size:90%">things</span>.</p></div>
<section id="S1" class="ltx_section"><h2 class="ltx_title">1 Intro</h2>
 <p>See <a href="#S2.SS1" class="ltx_ref">2.1</a> and <a href="#bib.bib1">[1]</a>.<span class="ltx_note ltx_role_footnote" id="footnote1"><sup class="ltx_note_mark">1</sup><span class="ltx_note_outer"><span class="ltx_note_content"><sup class="ltx_note_mark">1</sup><span class="ltx_tag ltx_tag_note">1</span>A note.</span></span></span></p>
 <img src="missing.png" class="ltx_graphics"/>
</section>
<section id="S2" class="ltx_section"><h2 class="ltx_title">2 Method</h2>
 <section id="S2.SS1" class="ltx_subsection"><h3 class="ltx_title">2.1 Details</h3><p>Back to <a href="#S1">1</a>.</p></section>
</section>
<section id="bib" class="ltx_bibliography"><h2 class="ltx_title">References</h2><ul><li id="bib.bib1">Ref.</li></ul></section>
</article></div><footer class="ltx_page_footer">LaTeXML</footer></div></body></html>"""


def _book(tmp_path):
    html = tmp_path / "html" / "index.html"
    html.parent.mkdir()
    html.write_text(HTML)
    main = tmp_path / "src" / "main.tex"
    main.parent.mkdir()
    main.write_text("\\documentclass{article}\\begin{document}\\end{document}")
    meta = Metadata("2401.00001", "A Paper", ["Ada Lovelace", "Alan Turing"], "Abstract.", "2024-01-01")
    return build_book(html, meta, main, tmp_path / "stage"), meta


def test_split_and_links(tmp_path):
    book, _ = _book(tmp_path)
    names = [c.filename for c in book.chapters]
    assert names == ["front.xhtml", "ch01.xhtml", "ch02.xhtml", "ch03.xhtml"]
    front, ch1, ch2, _ = (c.body for c in book.chapters)
    assert "Ada Lovelace, Alan Turing" in front and "messy authors" not in front
    assert "color" not in front and "font-size:90%" in front
    assert 'href="ch02.xhtml#S2.SS1"' in ch1
    assert 'href="ch03.xhtml#bib.bib1"' in ch1
    assert 'href="ch01.xhtml#S1"' in ch2
    assert "LaTeXML" not in "".join(c.body for c in book.chapters)
    assert "missing.png" not in ch1  # dropped, not left broken


def test_footnotes(tmp_path):
    book, _ = _book(tmp_path)
    ch1 = book.chapters[1].body
    assert 'epub:type="noteref"' in ch1 and 'href="#fn-footnote1"' in ch1
    assert 'epub:type="footnote"' in ch1 and "A note." in ch1


def test_toc(tmp_path):
    book, _ = _book(tmp_path)
    toc = book.chapters[2].toc
    assert toc.title == "2 Method" and toc.href == "ch02.xhtml"
    assert [(c.title, c.href) for c in toc.children] == [("2.1 Details", "ch02.xhtml#S2.SS1")]


def test_to_xhtml():
    out = to_xhtml('<p>a<br>b <img src="x.png"></p><aside epub:type="footnote" id="f">n &amp; m</aside>')
    assert "<br/>" in out and 'src="x.png"/>' in out
    assert 'epub:type="footnote"' in out and "n &amp; m" in out


def test_write_epub(tmp_path):
    book, meta = _book(tmp_path)
    out = write_epub(book, meta, tmp_path / "stage", tmp_path / "out.epub")
    with zipfile.ZipFile(out) as z:
        assert z.namelist()[0] == "mimetype"
        assert z.read("mimetype") == b"application/epub+zip"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        names = set(z.namelist())
        assert {"OEBPS/content.opf", "OEBPS/nav.xhtml", "OEBPS/toc.ncx", "OEBPS/cover.jpg"} <= names
        opf = z.read("OEBPS/content.opf").decode()
        assert "<dc:title>A Paper</dc:title>" in opf and "Ada Lovelace" in opf


def test_dangling_links_and_block_spans(tmp_path):
    html = HTML.replace(
        "<p>Back to",
        '<span class="ltx_transformed_outer" style="width:9pt"><span class="ltx_transformed_inner" '
        'style="transform:scale(0.5)"><table class="ltx_tabular"><tr><td>x</td></tr></table></span></span>'
        '<p><a href="#bib.bib99">[99]</a> Back to',
    )
    (tmp_path / "html").mkdir()
    (tmp_path / "html" / "index.html").write_text(html)
    main = tmp_path / "main.tex"
    main.write_text("\\documentclass{article}\\begin{document}\\end{document}")
    book = build_book(tmp_path / "html" / "index.html", Metadata("1", "T"), main, tmp_path / "stage")
    ch2 = book.chapters[2].body
    assert "bib.bib99" not in ch2 and "[99]" in ch2
    assert "transform:" not in ch2
    assert '<div class="ltx_transformed_inner"><table' in ch2
