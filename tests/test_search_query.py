from rejoin.search_query import file_filter_targets, parse_search_query


def test_parse_search_query_extracts_inline_filters():
    parsed = parse_search_query(
        'work file:"rejoin/app.py" ext:py op:edited basename:app.py '
        'active:true pinned:false tool:codex cwd:"/tmp/proj"'
    )

    assert parsed.q == "work"
    assert parsed.file == "rejoin/app.py"
    assert parsed.ext == "py"
    assert parsed.operation == "edited"
    assert parsed.basename == "app.py"
    assert parsed.active is True
    assert parsed.pinned is False
    assert parsed.tool == "codex"
    assert parsed.cwd == "/tmp/proj"


def test_parse_search_query_keeps_plain_query():
    parsed = parse_search_query("tailscale startup error")

    assert parsed.q == "tailscale startup error"
    assert parsed.file is None


def test_file_filter_targets_routes_path_and_filename_values():
    assert file_filter_targets("rejoin/app.py") == ("rejoin/app.py", None)
    assert file_filter_targets("app.py") == (None, "app.py")
    assert file_filter_targets(None) == (None, None)
