from rejoin.titler import _content_for, _content_hash, _fallback_title


class Row(dict):
    """Act like a sqlite3.Row for the titler's dict-style access."""
    def __getitem__(self, key):
        return super().__getitem__(key)


def test_content_hash_stable_and_change_detecting():
    a = Row(first_prompt="hello world", last_prompt="bye", codex_summary="")
    b = Row(first_prompt="hello world", last_prompt="bye", codex_summary="")
    c = Row(first_prompt="hello world!", last_prompt="bye", codex_summary="")
    ca = _content_hash(_content_for(a))
    cb = _content_hash(_content_for(b))
    cc = _content_hash(_content_for(c))
    assert ca == cb
    assert ca != cc


def test_content_for_skips_repeated_last_prompt():
    r = Row(first_prompt="hello", last_prompt="hello", codex_summary="")
    assert "LAST USER PROMPT" not in _content_for(r)


def test_fallback_title_truncates_and_ellipsizes():
    short = _fallback_title("short one")
    assert short == "short one"
    long = _fallback_title("x" * 200)
    assert long.endswith("…")
    assert len(long) <= 61


def test_fallback_title_empty():
    assert _fallback_title(None) == "(untitled session)"
    assert _fallback_title("") == "(untitled session)"
