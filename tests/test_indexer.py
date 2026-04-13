import json

from rejoin.indexer import parse_claude_session, parse_codex_session


def test_parse_claude_session_minimal(tmp_path):
    path = tmp_path / "abc-123.jsonl"
    events = [
        {"type": "user", "message": {"role": "user", "content": "hello"},
         "timestamp": "2026-04-01T00:00:00Z",
         "cwd": "/home/u/proj"},
        {"type": "assistant",
         "message": {"role": "assistant", "model": "claude-opus-4-6",
                     "content": [
                         {"type": "tool_use", "name": "Bash", "input": {}},
                         {"type": "text", "text": "ok"},
                     ]}},
        {"type": "last-prompt", "lastPrompt": "bye"},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events))
    rec = parse_claude_session(path)
    assert rec is not None
    assert rec.tool == "claude"
    assert rec.first_prompt == "hello"
    assert rec.last_prompt == "bye"
    assert rec.cwd == "/home/u/proj"
    assert rec.model == "claude-opus-4-6"
    assert rec.tool_call_count == 1
    assert rec.message_count == 2  # 1 user + 1 assistant


def test_parse_claude_session_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    rec = parse_claude_session(path)
    assert rec is not None
    assert rec.first_prompt is None


def test_parse_claude_session_malformed_lines(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"}}\n'
        'not-json\n'
        '{"type":"assistant","message":{"role":"assistant","content":[]}}\n'
    )
    rec = parse_claude_session(path)
    assert rec is not None
    assert rec.first_prompt == "hi"
    assert rec.message_count == 2


def test_parse_codex_session_minimal(tmp_path):
    path = tmp_path / "rollout-2026-04-07-abc.jsonl"
    events = [
        {"type": "session_meta",
         "timestamp": "2026-04-07T21:02:54Z",
         "payload": {"id": "019d69c1-abcd-efff-0000-111122223333",
                     "timestamp": "2026-04-07T21:02:54Z",
                     "cwd": "/home/u/proj"}},
        {"type": "response_item",
         "payload": {"type": "message", "role": "user",
                     "content": [{"type": "input_text", "text": "hi there"}]}},
        {"type": "response_item",
         "payload": {"type": "function_call", "name": "shell",
                     "arguments": "{}"}},
        {"type": "response_item",
         "payload": {"type": "message", "role": "assistant",
                     "content": [{"type": "output_text", "text": "ack"}]}},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events))
    rec = parse_codex_session(path)
    assert rec is not None
    assert rec.tool == "codex"
    assert rec.id == "019d69c1-abcd-efff-0000-111122223333"
    assert rec.cwd == "/home/u/proj"
    assert rec.first_prompt == "hi there"
    assert rec.tool_call_count == 1
    assert rec.message_count == 2


def test_parse_codex_session_recovers_id_from_filename(tmp_path):
    path = tmp_path / "rollout-2026-04-07T11-02-54-019d69c1-6142-7670-966f-61d8d2684158.jsonl"
    path.write_text('{"type":"response_item","payload":{"type":"message","role":"user","content":[]}}\n')
    rec = parse_codex_session(path)
    assert rec is not None
    assert rec.id == "019d69c1-6142-7670-966f-61d8d2684158"
