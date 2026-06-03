from pathlib import Path

from rejoin.brief import source_path_exists


def test_source_path_exists_handles_oserror(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: (_ for _ in ()).throw(OSError("nope")))

    assert source_path_exists("/bad/path") is None


def test_source_path_exists_virtual_path_is_unknown():
    assert source_path_exists("agent-sessions://pi/abc") is None
