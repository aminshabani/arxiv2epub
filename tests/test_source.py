import gzip
import io
import tarfile

import pytest

from arxiv2epub.source import SourceError, detect_kind, extract, find_main_tex


def make_tar(files: dict[str, bytes], compress=True) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz" if compress else "w") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


DOC = b"\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n"


def test_detect_kind():
    assert detect_kind(b"%PDF-1.5 ...") == "pdf"
    assert detect_kind(gzip.compress(b"%PDF-1.5")) == "pdf"
    assert detect_kind(gzip.compress(DOC)) == "gz"
    assert detect_kind(make_tar({"a.tex": DOC})) == "tar.gz"
    assert detect_kind(make_tar({"a.tex": DOC}, compress=False)) == "tar"
    assert detect_kind(DOC) == "tex"


def test_extract_single_gz(tmp_path):
    bundle = tmp_path / "src.bin"
    bundle.write_bytes(gzip.compress(DOC))
    out = extract(bundle, tmp_path / "x")
    assert (out / "main.tex").read_bytes() == DOC


def test_extract_pdf_only(tmp_path):
    bundle = tmp_path / "src.bin"
    bundle.write_bytes(b"%PDF-1.4 blah")
    with pytest.raises(SourceError, match="PDF"):
        extract(bundle, tmp_path / "x")


def test_find_main_prefers_begin_document(tmp_path):
    bundle = tmp_path / "src.bin"
    bundle.write_bytes(
        make_tar(
            {
                "sections/intro.tex": b"\\section{Intro}\n" * 500,
                "template.tex": b"% \\begin{document}\n\\documentclass{article}\n" + b"x" * 9000,
                "neurips.tex": DOC,
            }
        )
    )
    root = extract(bundle, tmp_path / "x")
    assert find_main_tex(root).name == "neurips.tex"


def test_find_main_prefers_main_name(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    (root / "supp.tex").write_bytes(DOC + b"%" * 5000)
    (root / "main.tex").write_bytes(DOC)
    assert find_main_tex(root).name == "main.tex"


def test_find_main_none(tmp_path):
    (tmp_path / "a.tex").write_text("\\section{x}")
    with pytest.raises(SourceError):
        find_main_tex(tmp_path)


def test_check_convertible_rejects_pdf_wrapper(tmp_path):
    from arxiv2epub.source import check_convertible

    wrapper = tmp_path / "arxiv.tex"
    wrapper.write_text(
        "\\documentclass{article}\\usepackage{pdfpages}\n\\begin{document}\n"
        "\\includepdf[pages=1-last]{paper.pdf}\n\\end{document}\n"
    )
    with pytest.raises(SourceError, match="embeds a PDF"):
        check_convertible(wrapper)
    real = tmp_path / "main.tex"
    real.write_bytes(DOC)
    check_convertible(real)


def test_gzip_bomb_rejected(tmp_path, monkeypatch):
    import arxiv2epub.source as source

    monkeypatch.setattr(source, "MAX_UNPACKED_BYTES", 1000)
    bundle = tmp_path / "src.bin"
    bundle.write_bytes(gzip.compress(b"\0" * 100_000))
    with pytest.raises(SourceError, match="expands"):
        extract(bundle, tmp_path / "x")


def test_tar_limits(tmp_path, monkeypatch):
    import arxiv2epub.source as source

    bundle = tmp_path / "src.bin"
    bundle.write_bytes(make_tar({"main.tex": DOC, "big.dat": b"\0" * 5000}, compress=False))
    monkeypatch.setattr(source, "MAX_UNPACKED_BYTES", 4000)
    with pytest.raises(SourceError, match="expands"):
        extract(bundle, tmp_path / "x")
    monkeypatch.setattr(source, "MAX_UNPACKED_BYTES", 1 << 20)
    monkeypatch.setattr(source, "MAX_MEMBERS", 1)
    with pytest.raises(SourceError, match="files"):
        extract(bundle, tmp_path / "y")
