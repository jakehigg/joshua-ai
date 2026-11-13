import pytest
from joshua_shared.item_url import check_item_url, is_safe_item_url


@pytest.mark.parametrize(
    "url",
    ["https://a.example/x?y=1", "HTTP://a.example", "/documents/alex/x", "/assignments/7#top"],
)
def test_safe_urls(url: str) -> None:
    assert is_safe_item_url(url)
    assert check_item_url(f"  {url} ") == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        " JavaScript:alert(1)",
        "data:text/html,x",
        "vbscript:x",
        "file:///etc/passwd",
        "//evil.example/a",
        "/\\evil.example",
        "https:///nohost",
        "relative/path",
        "http://a.example/\nx",
    ],
)
def test_unsafe_urls(url: str) -> None:
    assert not is_safe_item_url(url)
    with pytest.raises(ValueError):
        check_item_url(url)


def test_empty_is_none() -> None:
    assert check_item_url(None) is None
    assert check_item_url("  ") is None
