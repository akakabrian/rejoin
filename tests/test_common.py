from rejoin.common import (
    iso_to_epoch,
    iter_jsonl,
    short_cwd,
    text_of,
    utcnow_iso,
    uuid_from_stem,
)


def test_iso_to_epoch_handles_z_suffix():
    assert iso_to_epoch("2026-04-07T21:02:54Z") > 0


def test_iso_to_epoch_handles_bad_input():
    assert iso_to_epoch(None) == 0.0
    assert iso_to_epoch("") == 0.0
    assert iso_to_epoch("not-a-date") == 0.0


def test_short_cwd_replaces_home():
    # home is resolved at import time, so we just check the shape
    assert short_cwd("/tmp/foo") == "/tmp/foo"
    assert short_cwd(None) == ""


def test_text_of_handles_all_shapes():
    assert text_of("plain") == "plain"
    assert text_of([{"type": "text", "text": "hi"}]) == "hi"
    assert text_of([{"type": "input_text", "text": "a"},
                    {"type": "output_text", "text": "b"}]) == "a\nb"
    assert text_of(None) == ""
    assert text_of(42) == ""


def test_uuid_from_stem_extracts_uuid():
    stem = "rollout-2026-04-07T11-02-54-019d69c1-6142-7670-966f-61d8d2684158"
    assert uuid_from_stem(stem) == "019d69c1-6142-7670-966f-61d8d2684158"
    assert uuid_from_stem("no-uuid-here") == "no-uuid-here"


def test_iter_jsonl_skips_blank_and_malformed(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a":1}\n\n{bad\n{"b":2}\n')
    rows = list(iter_jsonl(p))
    assert rows == [{"a": 1}, {"b": 2}]


def test_utcnow_iso_is_iso_with_tz():
    s = utcnow_iso()
    assert "T" in s and (s.endswith("+00:00") or s.endswith("Z"))
