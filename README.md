# arxiv2epub

Convert an arXiv paper into an EPUB that reads well on a Kindle (or any e-reader).

```console
$ arxiv2epub 1706.03762
[1/6] Fetching metadata for 1706.03762
      Attention Is All You Need
[2/6] Downloading source
      main file: ms.tex
[3/6] Typesetting with LaTeX (TikZ pictures, wide tables)
      0 TikZ pictures, 5 wide tables as images
[4/6] Converting with LaTeXML (this can take a minute)
[5/6] Rendering math and figures
      10 chapters, 80 formula images, 12 figures
[6/6] Writing EPUB

Done in 36s: ./1706.03762.epub (1.4 MB)
```

Then send the file to your Kindle with [Send to Kindle](https://www.amazon.com/sendtokindle)
(web, desktop app, or email it to your `@kindle.com` address).

## How it works

1. Downloads the LaTeX source from `arxiv.org/src/<id>` (cached in `~/.cache/arxiv2epub`)
   and metadata from the arXiv API.
2. Typesets some pieces with real LaTeX before conversion:
   - TikZ/pgfplots pictures, because LaTeXML's pgf support is very slow and often inaccurate
   - tables too wide for an e-reader screen. These become images you can long-press to zoom.
     Narrow tables stay as text.
3. Converts the document with [LaTeXML](https://math.nist.gov/~BMiller/LaTeXML/), the same
   engine behind arXiv's HTML view. PDF and EPS figures are rasterized with PyMuPDF.
   Lightweight stand-in bindings (`src/arxiv2epub/assets/stubs/`) replace packages that
   LaTeXML otherwise interprets from source for minutes:
   - `pgf`/`tikz`, after the pictures have been pre-rendered
   - `xparse` and `xpatch`, which pull in the expl3 kernel
   - `babel`
   Each stub is only used when the paper doesn't need the real package.
4. Renders **every formula with real LaTeX, using the paper's own preamble**, so custom
   macros look exactly like the PDF. Each formula becomes an image sized in `em`, so it
   scales with the reader's font size, and inline math sits on the text baseline.
   Kindle's MathML support is unreliable, which is why formulas are images.
5. Splits the document into one chapter per section, with a navigable table of contents,
   pop-up footnotes, working cross-references and a generated cover. It then writes
   an EPUB3 that passes `epubcheck`.

## Install

Requirements are macOS or Linux, Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

```sh
brew install latexml              # the converter
brew install --cask mactex-no-gui # pdflatex (skip if you already have TeX Live/MacTeX)
brew install ghostscript          # optional: EPS figures
brew install epubcheck            # optional: validate output

git clone https://github.com/aminshabani/arxiv2epub.git && cd arxiv2epub
uv tool install .                 # puts `arxiv2epub` on your PATH
arxiv2epub doctor                 # checks the dependencies
```

To run it from the checkout without installing, use `uv run arxiv2epub <id>`.

## Usage

```
arxiv2epub <id-or-url> [-o DIR] [--timeout SECONDS] [--no-cache] [--keep-work] [-v]
```

The paper can be given in any of these forms:
- a new-style ID: `2401.12345` or `2401.12345v2`
- an old-style ID: `hep-th/9901001`
- an `arxiv.org/abs/…`, `/pdf/…` or `/html/…` URL

The flags are:
- `--keep-work` keeps the intermediate files in `~/.cache/arxiv2epub/<id>/work` for debugging.
  These include the LaTeXML log and the math and TikZ build logs.
- `-v` shows detailed progress.

## Limitations

- **The paper must have LaTeX source.** PDF-only submissions and plain-TeX papers (for example
  old `harvmac` hep-th papers) can't be converted.
- **Formulas that fail to render are shown as their TeX source.** This happens when a formula
  doesn't compile on its own, and is usually rare.
- **`tikzcd` diagrams inside math still go through LaTeXML's pgf support.** That is slow,
  so pgf isn't stubbed out for those papers.
- **A paper that programs in expl3 itself (`\ExplSyntaxOn`) is slow.** LaTeXML has to load
  the whole LaTeX3 kernel, so raise `--timeout` for these.
- **Tables defined inside macros stay as HTML**, as do tables LaTeX can't typeset on their
  own. On a small screen these can be cramped.

## License

MIT. See [LICENSE](LICENSE).

## Development

```sh
uv run pytest            # unit tests
uv run pytest -m e2e     # end-to-end conversion (network + LaTeXML + TeX)
```
