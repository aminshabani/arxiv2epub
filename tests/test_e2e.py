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
