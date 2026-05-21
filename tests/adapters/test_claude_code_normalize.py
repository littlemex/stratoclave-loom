"""Unit tests for ClaudeCodeBackend.normalize."""

from __future__ import annotations

import json

from stratoclave_loom.adapters.claude_code import ClaudeCodeBackend


def _line(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False) + "\n"


def test_normalize_text_block() -> None:
    backend = ClaudeCodeBackend()
    raw = _line(
        {
            "uuid": "u1",
            "sessionId": "s1",
            "timestamp": "2026-01-01T00:00:00Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello world"}],
            },
        }
    )
    turns = backend.normalize(raw, seq=0)
    assert len(turns) == 1
    t = turns[0]
    assert t.role == "assistant"
    assert t.text_content == "hello world"
    assert t.session_id == "s1"
    assert t.tool_name is None


def test_normalize_tool_use_block_is_renamed_to_normalized_namespace() -> None:
    backend = ClaudeCodeBackend()
    raw = _line(
        {
            "uuid": "u2",
            "sessionId": "s1",
            "message": {
                "role": "assistant",
                "content": [{"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}}],
            },
        }
    )
    turns = backend.normalize(raw, seq=1)
    assert len(turns) == 1
    t = turns[0]
    assert t.role == "tool_use"
    assert t.tool_name == "shell.run"
    assert t.tool_input == {"cmd": "ls"}


def test_normalize_returns_empty_for_unrecognized_lines() -> None:
    backend = ClaudeCodeBackend()
    assert backend.normalize("", seq=0) == []
    assert backend.normalize("not json", seq=0) == []
    assert backend.normalize(_line({"unrelated": 1}), seq=0) == []
