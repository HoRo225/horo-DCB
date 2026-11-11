import pytest

from horo_dcb.render import answer_chunks, search_text, shorten, summary_title


def test_short_answer_is_one_chunk():
    assert answer_chunks("天空是藍色的。") == ["天空是藍色的。"]


def test_empty_answer_has_no_chunks():
    assert answer_chunks("") == []


def test_ascii_answer_splits_at_4000_characters():
    text = "a" * 9000
    chunks = answer_chunks(text)
    assert [len(chunk) for chunk in chunks] == [4000, 4000, 1000]
    assert "".join(chunks) == text


def test_multibyte_answer_splits_at_9000_bytes():
    text = "藍" * 3500
    chunks = answer_chunks(text)
    assert [len(chunk.encode()) for chunk in chunks] == [9000, 1500]
    assert "".join(chunks) == text


def test_split_prefers_a_newline_in_the_second_half():
    text = "a" * 3000 + "\n" + "b" * 3000
    assert answer_chunks(text) == ["a" * 3000 + "\n", "b" * 3000]


def test_split_ignores_a_newline_in_the_first_half():
    text = "a" * 100 + "\n" + "b" * 5000
    chunks = answer_chunks(text)
    assert len(chunks[0]) == 4000
    assert "".join(chunks) == text


def test_whitespace_only_chunk_is_dropped():
    text = "a" * 3000 + "\n" + " " * 3999
    assert answer_chunks(text) == ["a" * 3000 + "\n"]


def test_shorten_collapses_whitespace():
    assert shorten("  a \n\n b\tc ", 80) == "a b c"


def test_shorten_cuts_to_the_limit_with_an_ellipsis():
    assert shorten("abcdefghij", 5) == "abcd…"


def test_shorten_keeps_text_at_the_limit():
    assert shorten("abcde", 5) == "abcde"


def test_summary_title_uses_the_bold_heading():
    assert summary_title("**Checking the weather**\n\nI will search for Taipei.") == "Checking the weather"


def test_summary_title_falls_back_to_the_first_line():
    assert summary_title("\nFirst line\nSecond line") == "First line"


def test_summary_title_is_at_most_100_characters():
    assert len(summary_title("x" * 300)) == 100


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"action": {"type": "openPage", "url": "https://www.cwa.gov.tw/V8/C/W/County/index.html"}}, "瀏覽 www.cwa.gov.tw"),
        ({"action": {"type": "findInPage", "pattern": "氣溫"}}, "在網頁中尋找「氣溫」"),
        ({"action": {"type": "search", "query": "台北 天氣"}}, "搜尋「台北 天氣」"),
        ({"action": {"type": "search", "queries": ["第一個", "第二個"]}}, "搜尋「第一個」"),
        ({"query": "舊格式查詢"}, "搜尋「舊格式查詢」"),
        ({}, "搜尋網路"),
    ],
    ids=["open-page", "find-in-page", "query", "queries", "item-query", "nothing"],
)
def test_search_text(item, expected):
    assert search_text(item) == expected
