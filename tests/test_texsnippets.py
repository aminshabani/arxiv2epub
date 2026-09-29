from arxiv2epub.mathrender import Formula, formula_body
from arxiv2epub.texsnippets import engine_for, parse_log, scrape_macros, split_document

LOG = """\
A2E-FONTSIZE=10
A2E-BEGIN-1
A2E-DIMS-1=6.83331pt,0.0pt,5.72218pt
[1{/usr/local/texlive/pdftex.map}]
A2E-BEGIN-2
! Undefined control sequence.
l.12 \\foo
A2E-DIMS-2=1.0pt,0.0pt,1.0pt
[2]
A2E-BEGIN-3
A2E-DIMS-3=7.5pt,2.5pt,30.0pt
[3 <./fig.png>]
A2E-BEGIN-END
"""


def test_parse_log_skips_errored_snippets():
    out = parse_log(LOG)
    assert out == {1: (1, 6.83331, 0.0, 5.72218), 3: (3, 7.5, 2.5, 30.0)}


def test_parse_log_truncated_run():
    truncated = LOG.split("A2E-BEGIN-3")[0]
    assert set(parse_log(truncated)) == {1}


def test_split_document_ignores_commented_begin():
    tex = "\\documentclass{article}\n% \\begin{document}\n\\def\\x{1}\n\\begin{document}\nhi"
    pre, rest = split_document(tex)
    assert pre.endswith("\\def\\x{1}\n")
    assert rest.startswith("\\begin{document}")


def test_scrape_macros():
    pre = r"""
\documentclass{article}
\usepackage{amsmath}
\newcommand{\R}{\mathbb{R}}
\newcommand\vx{\mathbf{x}}
\renewcommand*{\vec}[1]{\boldsymbol{#1}}
\DeclareMathOperator*{\argmax}{arg\,max}
\def\E#1{\mathbb{E}\left[#1\right]}
\let\oldphi\phi % comment \newcommand{\bad}{}
\newcommand{\nested}[2][x]{\frac{#1}{#2}}
"""
    out = scrape_macros(pre)
    assert r"\newcommand{\R}{\mathbb{R}}" in out
    assert r"\newcommand\vx{\mathbf{x}}" in out
    assert r"\renewcommand*{\vec}[1]{\boldsymbol{#1}}" in out
    assert r"\DeclareMathOperator*{\argmax}{arg\,max}" in out
    assert r"\def\E#1{\mathbb{E}\left[#1\right]}" in out
    assert r"\let\oldphi\phi" in out
    assert r"\newcommand{\nested}[2][x]{\frac{#1}{#2}}" in out
    assert r"\bad" not in out
    assert "usepackage" not in out


def test_engine_for():
    assert engine_for(r"\usepackage{amsmath}") == "pdflatex"
    assert engine_for(r"\usepackage[no-math]{fontspec}") == "xelatex"
    assert engine_for(r"% \usepackage{fontspec}") == "pdflatex"


def test_formula_body():
    assert formula_body(Formula("x^2", False)) == "$x^2$"
    assert formula_body(Formula(r"a=b\label{eq:1}\nonumber", True)) == r"$\displaystyle a=b$"
    assert formula_body(Formula(r"a&=b\\c&=d", True)) == (
        r"$\displaystyle \begin{aligned}a&=b\\c&=d\end{aligned}$"
    )
    assert formula_body(Formula(r"\begin{split}a&=b\end{split}", True)) == (
        r"$\displaystyle \begin{aligned}a&=b\end{aligned}$"
    )
    assert formula_body(Formula(r"50\% of x", False)) == r"$50\% of x$"
