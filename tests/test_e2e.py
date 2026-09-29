"""End-to-end conversion; needs network + LaTeXML + TeX. Run with: pytest -m e2e"""

import shutil
import subprocess
import zipfile

import pytest

from arxiv2epub.cli import main

pytestmark = pytest.mark.e2e


@pytest.mark.parametrize("paper", ["1706.03762"])
def test_convert(paper, tmp_path):
    assert main([paper, "-o", str(tmp_path)]) == 0
    epub = tmp_path / f"{paper}.epub"
    with zipfile.ZipFile(epub) as z:
        names = z.namelist()
    assert any(n.startswith("OEBPS/math/") for n in names)
    assert any(n.startswith("OEBPS/images/") for n in names)
    if shutil.which("epubcheck"):
        r = subprocess.run(["epubcheck", str(epub)], capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr


def test_convert_native_matches_sandbox(tmp_path):
    """Both paths produce the same book (needs Docker running and the local TeX tools)."""
    from arxiv2epub import sandbox

    try:
        sandbox.check_available()
    except sandbox.SandboxError as e:
        pytest.skip(str(e))
    books = {}
    for mode, flags in (("sandbox", []), ("native", ["--no-sandbox"])):
        out = tmp_path / mode
        assert main(["1706.03762", "-o", str(out), *flags]) == 0
        with zipfile.ZipFile(out / "1706.03762.epub") as z:
            books[mode] = sorted(n for n in z.namelist() if n.endswith(".xhtml"))
    assert books["sandbox"] == books["native"]
