from arxiv2epub.latexml import STUBS_DIR, stub_paths


def _names(src):
    return [p.name for p in stub_paths(src)]


def test_stubs_all_when_safe(tmp_path):
    (tmp_path / "main.tex").write_text("\\usepackage{tikz,xparse}\\tikzset{x}\n% \\begin{tikzpicture}\n")
    assert _names(tmp_path) == ["common", "tikz", "xparse"]


def test_stubs_skip_when_real_packages_needed(tmp_path):
    (tmp_path / "main.tex").write_text("$\\begin{tikzcd} A \\arrow[r] & B \\end{tikzcd}$")
    (tmp_path / "macros.sty").write_text("\\ExplSyntaxOn \\cs_new:Npn \\foo {} \\ExplSyntaxOff")
    assert _names(tmp_path) == ["common"]


def test_stub_files_exist():
    for d in ("common", "tikz", "xparse"):
        assert list((STUBS_DIR / d).glob("*.ltxml"))
