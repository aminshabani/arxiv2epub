from arxiv2epub.latexml import STUBS_DIR, stub_paths


def _names(src):
    return [p.name for p in stub_paths(src)]


def test_stubs_all_when_safe(tmp_path):
    (tmp_path / "main.tex").write_text("\\usepackage{tikz,xparse}\\tikzset{x}\n% \\begin{tikzpicture}\n")
    assert _names(tmp_path) == ["common", "tikz", "expl3"]


def test_stubs_skip_when_real_packages_needed(tmp_path):
    (tmp_path / "main.tex").write_text("$\\begin{tikzcd} A \\arrow[r] & B \\end{tikzcd}$")
    (tmp_path / "macros.sty").write_text("\\ExplSyntaxOn \\cs_new:Npn \\foo {} \\ExplSyntaxOff")
    assert _names(tmp_path) == ["common"]


def test_stub_files_exist():
    for d in ("common", "tikz", "expl3"):
        assert list((STUBS_DIR / d).glob("*.ltxml"))


def test_drop_expl3_packages(tmp_path, monkeypatch):
    from arxiv2epub import latexml

    builtin = tmp_path / "Package"
    builtin.mkdir()
    (builtin / "siunitx.sty.ltxml").write_text("")
    monkeypatch.setattr(latexml, "latexml_binding_dir", lambda: builtin)
    monkeypatch.setattr(latexml, "_is_expl3_package", lambda p: p in {"fontawesome5", "siunitx", "fancyx"})
    src = tmp_path / "src"
    src.mkdir()
    (src / "fancyx.sty").write_text("")  # shipped with the paper: LaTeXML loads it itself
    main = src / "main.tex"
    main.write_text(
        "\\usepackage{amsmath,fontawesome5 , siunitx}\n"
        "\\usepackage[solid]{fontawesome5}\n"
        "% \\usepackage{fontawesome5}\n"
        "\\usepackage{fancyx}\n"
    )
    assert latexml.drop_expl3_packages(src, []) == ["fontawesome5"]
    assert main.read_text() == (
        "\\usepackage{amsmath,siunitx}\n\n% \\usepackage{fontawesome5}\n\\usepackage{fancyx}\n"
    )


def test_replace_pictures():
    from lxml import etree

    from arxiv2epub.latexml import LTX_NS, replace_pictures

    xml = (
        f'<document xmlns="{LTX_NS}"><para><p>A '
        '<picture tex="\\begin{overpic}[width=5pt]{figs/f.png}\\put(1,2){x}\\end{overpic}" '
        'xml:id="p1"/> B <picture tex="\\begin{picture}(1,1)\\end{picture}"/> C</p></para></document>'
    )
    tree = etree.ElementTree(etree.fromstring(xml))
    assert replace_pictures(tree) == 2
    out = etree.tostring(tree, encoding="unicode")
    assert "picture" not in out
    assert 'graphic="figs/f.png"' in out and 'xml:id="p1"' in out
    assert "A <graphics" in out and "/> B  C</p>" in out


def test_bibliography_args(tmp_path):
    from arxiv2epub.latexml import BINDINGS_DIR, bibliography_args

    main = tmp_path / "main.tex"
    main.write_text("\\bibliography{refs}")
    (tmp_path / "refs.bib").write_text("@misc{a,title={A}}")
    assert bibliography_args(main) == []  # no .bbl: LaTeXML processes the .bib
    (tmp_path / "main.bbl").write_text("\\begin{thebibliography}{1}\\end{thebibliography}")
    assert bibliography_args(main) == [f"--path={BINDINGS_DIR}", "--preload=a2e_usebbl.sty"]
    assert (BINDINGS_DIR / "a2e_usebbl.sty.ltxml").is_file()
