from arxiv2epub.prerender import _setup_commands, find_pictures


def test_find_pictures_nested_and_commented():
    text = (
        "a\n% \\begin{tikzpicture} commented \\end{tikzpicture}\n"
        "\\begin{tikzpicture}\\node{\\begin{tikzpicture}x\\end{tikzpicture}};\\end{tikzpicture}\n"
        "b \\begin{tikzpicture}[scale=2]\\draw (0,0);\\end{tikzpicture}"
    )
    spans = find_pictures(text)
    assert len(spans) == 2
    first = text[spans[0][0]:spans[0][1]]
    assert first.startswith("\\begin{tikzpicture}\\node") and first.endswith("};\\end{tikzpicture}")
    assert text[spans[1][0]:spans[1][1]].startswith("\\begin{tikzpicture}[scale=2]")


def test_find_pictures_unbalanced():
    assert find_pictures("\\begin{tikzpicture} never closed") == []


def test_setup_commands_arity():
    text = (
        "\\pgfplotsset{small,width=5cm}\n\\begin{figure}\n"
        "\\pgfplotstableread{data.csv}\\mytab\n"
        "\\definecolor{c1}{RGB}{1,2,3} text\n"
        "\\tikzstyle{box}=[draw, fill=red] more\n"
        "% \\tikzset{ignored}\n"
    )
    assert _setup_commands(text) == [
        "\\pgfplotsset{small,width=5cm}",
        "\\pgfplotstableread{data.csv}\\mytab",
        "\\definecolor{c1}{RGB}{1,2,3}",
        "\\tikzstyle{box}=[draw, fill=red]",
    ]


def test_count_columns():
    from arxiv2epub.prerender import count_columns

    assert count_columns(r"\begin{tabular}{l|c|r}a\end{tabular}") == 3
    assert count_columns(r"\begin{tabular}{@{}l@{\hspace{2mm}}p{3cm}>{\bfseries}c@{}}\end{tabular}") == 3
    assert count_columns(r"\begin{tabular}{l*{4}{c}}\end{tabular}") == 5
    assert count_columns(r"\begin{tabular}[t]{|l|X|}\end{tabular}") == 2
    assert count_columns(r"\begin{tabularx}{\linewidth}{lXX}\end{tabularx}") == 3
    assert count_columns(r"\begin{tabular*}{\textwidth}{@{\extracolsep{\fill}}lcc}\end{tabular*}") == 3
    assert count_columns(r"\begin{tabular}{L}\end{tabular}") == 1
