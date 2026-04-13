from __future__ import annotations

import shlex
import shutil
import subprocess


class MissingBinary(RuntimeError):
    """Raised when a required external binary (tmux, claude, codex, pi) is absent."""


def _require(binary: str, context: str) -> None:
    if shutil.which(binary) is None:
        raise MissingBinary(
            f"'{binary}' not found on PATH — required for {context}. "
            f"Install it first, or use the 'copy command' button to run the "
            f"command in a terminal that has it."
        )


def resume_command(tool: str, session_id: str, cwd: str | None) -> str:
    """Return the shell command a user would run to resume this session."""
    # Leave "~" unquoted so the shell expands it; quote everything else.
    cd_target = "~" if not cwd else shlex.quote(cwd)
    if tool == "claude":
        inner = f"claude --resume {shlex.quote(session_id)}"
    elif tool == "codex":
        inner = f"codex resume {shlex.quote(session_id)}"
    elif tool == "pi":
        inner = f"pi {shlex.quote(session_id)}"
    elif tool == "hermes":
        inner = f"hermes --resume {shlex.quote(session_id)}"
    elif tool == "openclaw":
        # OpenClaw doesn't expose a `resume` subcommand; closest interactive
        # flow is `openclaw agent --session-id <id>` with a follow-up message.
        # We default to 'continue' which the user can edit in the tmux window.
        inner = f"openclaw agent --session-id {shlex.quote(session_id)} -m continue"
    elif tool == "opencode":
        # OpenCode doesn't expose session IDs on the CLI (no --resume flag).
        # Best we can do is drop into the project dir and relaunch; the user
        # picks the session from OpenCode's own UI.
        inner = "opencode"
    else:
        raise ValueError(f"unknown tool: {tool}")
    return f"cd {cd_target} && {inner}"


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
    _require("tmux", "launching a rejoin session")
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
