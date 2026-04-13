import pytest

from rejoin.resume import resume_command, tmux_session_name


def test_resume_command_claude():
    cmd = resume_command("claude", "abc-123", "/home/u/proj")
    assert "cd /home/u/proj" in cmd
    assert "claude --resume abc-123" in cmd


def test_resume_command_codex():
    cmd = resume_command("codex", "abc-123", "/home/u/proj")
    assert "codex resume abc-123" in cmd


def test_resume_command_pi():
    cmd = resume_command("pi", "abc-123", None)
    assert "pi abc-123" in cmd
    # falls back to ~ when cwd is None
    assert "cd ~" in cmd


def test_resume_command_opencode_has_no_session_arg():
    cmd = resume_command("opencode", "abc-123", "/tmp")
    assert "opencode" in cmd
    assert "abc-123" not in cmd  # OpenCode doesn't take session on CLI


def test_resume_command_unknown_tool_raises():
    with pytest.raises(ValueError):
        resume_command("nope", "abc", None)


def test_resume_command_shell_quotes_spaces_in_cwd():
    cmd = resume_command("claude", "abc", "/home/u/Paa Prefab CRM")
    assert "'/home/u/Paa Prefab CRM'" in cmd


def test_tmux_session_name_uses_short_id():
    assert tmux_session_name("claude", "abcdef0123456789") == "sess-claude-abcdef01"
