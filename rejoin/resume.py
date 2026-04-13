from __future__ import annotations

import shlex
import subprocess


def resume_command(tool: str, session_id: str, cwd: str | None) -> str:
    """Return the shell command a user would run to resume this session."""
    cwd = cwd or "~"
    if tool == "claude":
        inner = f"claude --resume {shlex.quote(session_id)}"
    elif tool == "codex":
        inner = f"codex resume {shlex.quote(session_id)}"
    else:
        raise ValueError(f"unknown tool: {tool}")
    return f"cd {shlex.quote(cwd)} && {inner}"


def tmux_session_name(tool: str, session_id: str) -> str:
    short = session_id[:8]
    return f"sess-{tool}-{short}"


def tmux_session_exists(name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
    )
    return result.returncode == 0


def launch_tmux(tool: str, session_id: str, cwd: str | None) -> dict:
    name = tmux_session_name(tool, session_id)
    cmd = resume_command(tool, session_id, cwd)
    created = False
    if not tmux_session_exists(name):
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "bash", "-lc", cmd],
            check=True,
        )
        created = True
    return {
        "tmux_name": name,
        "attach": f"tmux attach -t {shlex.quote(name)}",
        "command": cmd,
        "created": created,
    }
