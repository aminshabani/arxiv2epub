import pytest

from arxiv2epub.arxiv import ArxivError, parse_id, safe_name


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2401.12345", "2401.12345"),
        ("2401.12345v2", "2401.12345v2"),
        ("1706.03762", "1706.03762"),
        ("0704.0001", "0704.0001"),
        ("hep-th/9711200", "hep-th/9711200"),
        ("math.AG/0601001", "math.AG/0601001"),
        ("arXiv:2401.12345", "2401.12345"),
        ("https://arxiv.org/abs/2401.12345v3", "2401.12345v3"),
        ("https://arxiv.org/pdf/2401.12345.pdf", "2401.12345"),
        ("https://arxiv.org/pdf/2401.12345v1", "2401.12345v1"),
        ("https://arxiv.org/html/2401.12345v1", "2401.12345v1"),
        ("http://arxiv.org/abs/hep-th/9711200", "hep-th/9711200"),
        ("  2401.12345 \n", "2401.12345"),
    ],
)
def test_parse_id(text, expected):
    assert parse_id(text) == expected


@pytest.mark.parametrize("bad", ["", "hello", "2401.123", "https://example.com/abs/2401.12345x"])
def test_parse_id_rejects(bad):
    with pytest.raises(ArxivError):
        parse_id(bad)


def test_safe_name():
    assert safe_name("hep-th/9711200") == "hep-th_9711200"
