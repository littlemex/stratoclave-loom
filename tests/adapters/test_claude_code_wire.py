"""Wire-level tests for ClaudeCodeBackend using a Python stub binary.

These tests exercise the subprocess plumbing without requiring the real
``claude`` CLI: send_message dispatches a stream of AcpChunks, cancel
terminates the in-flight subprocess, close is idempotent, and CLI session
ids captured from system/init are propagated to the resume argument on
subsequent turns.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from stratoclave_loom.adapters.claude_code import ENV_CLAUDE_CLI, ClaudeCodeBackend
from stratoclave_loom.core.errors import AdapterError
from stratoclave_loom.core.types import AcpChunk, BackendConfig, PermissionRequest

_STUB_PATH = Path(__file__).parent / "_claude_stub.py"


def _stub_argv() -> str:
    """Return the executable path to use as STRATOCLAVE_LOOM_CLAUDE_CLI.

    We point the env var at the current Python interpreter, then prepend
    the stub script via ``BackendConfig.extra['extra_cli_args']`` is not
    quite right — the adapter places extras at the *end* of argv. So
    instead we wrap the stub in a one-line shell launcher written to a
    temp file by each test that needs it.
    """
    return sys.executable


def _make_launcher(tmp_path: Path) -> Path:
    """Create an executable shim that runs ``python _claude_stub.py "$@"``.

    The adapter's argv flags (e.g. ``--print``, ``--output-format``) come
    after the executable, so we cannot use ``sys.executable`` directly —
    Python would try to run ``--print`` as a script. The shim drops those
    flags by ignoring all of its arguments and just running the stub.
    """
    launcher = tmp_path / "claude-shim"
    launcher.write_text(
        f'#!/usr/bin/env bash\nexec {sys.executable} {_STUB_PATH} "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    return launcher


def _write_script(path: Path, lines: list[dict[str, object]]) -> Path:
    path.write_text(
        "\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n",
        encoding="utf-8",
    )
    return path


async def _drain(stream: AsyncIterator[AcpChunk]) -> list[AcpChunk]:
    chunks: list[AcpChunk] = []
    async for chunk in stream:
        chunks.append(chunk)
    return chunks


@pytest.mark.asyncio
async def test_send_message_emits_text_deltas_and_end_turn(tmp_path: Path) -> None:
    launcher = _make_launcher(tmp_path)
    script = _write_script(
        tmp_path / "script.jsonl",
        [
            {"type": "system", "subtype": "init", "session_id": "cli-sess-1"},
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hel"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "lo"},
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "stop_reason": "end_turn",
                "duration_ms": 12,
                "total_cost_usd": 0.001,
            },
        ],
    )

    backend = ClaudeCodeBackend()
    cfg = BackendConfig(
        backend="claude_code",
        cwd=str(tmp_path),
        env={
            "CLAUDE_STUB_SCRIPT": str(script),
        },
        extra={"claude_cli": str(launcher)},
    )
    await backend.initialize("loom-sess-1", cfg, capabilities={})
    stream = await backend.send_message("loom-sess-1", "hi")
    chunks = await _drain(stream)
    await backend.close("loom-sess-1")

    types = [c.chunk_type for c in chunks]
    assert types == ["text_delta", "text_delta", "end_turn"]
    assert chunks[0].content["text"] == "Hel"
    assert chunks[1].content["text"] == "lo"
    assert chunks[2].content["stop_reason"] == "end_turn"
    # seq is monotonic and stamped per chunk.
    assert [c.seq for c in chunks] == [0, 1, 2]


@pytest.mark.asyncio
async def test_system_init_session_id_is_used_for_resume_on_next_turn(tmp_path: Path) -> None:
    launcher = _make_launcher(tmp_path)
    script1 = _write_script(
        tmp_path / "first.jsonl",
        [
            {"type": "system", "subtype": "init", "session_id": "cli-sess-resume"},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "stop_reason": "end_turn",
            },
        ],
    )
    argv_record = tmp_path / "argv2.json"
    script2 = _write_script(
        tmp_path / "second.jsonl",
        [
            {"type": "system", "subtype": "init", "session_id": "cli-sess-resume"},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "stop_reason": "end_turn",
            },
        ],
    )

    backend = ClaudeCodeBackend()
    cfg1 = BackendConfig(
        backend="claude_code",
        cwd=str(tmp_path),
        env={"CLAUDE_STUB_SCRIPT": str(script1)},
        extra={"claude_cli": str(launcher)},
    )
    await backend.initialize("loom-A", cfg1, capabilities={})
    await _drain(await backend.send_message("loom-A", "first turn"))

    # Second turn — env now records argv to confirm --resume is appended.
    cfg2 = BackendConfig(
        backend="claude_code",
        cwd=str(tmp_path),
        env={
            "CLAUDE_STUB_SCRIPT": str(script2),
            "CLAUDE_STUB_RECORD_ARGV": str(argv_record),
        },
        extra={"claude_cli": str(launcher)},
    )
    # Update the live session to use the new env without re-initialising.
    backend._sessions["loom-A"].config = cfg2
    await _drain(await backend.send_message("loom-A", "second turn"))
    await backend.close("loom-A")

    argv = json.loads(argv_record.read_text())
    # The shim forwards args after position 0; --resume <id> must be present.
    assert "--resume" in argv
    resume_index = argv.index("--resume")
    assert argv[resume_index + 1] == "cli-sess-resume"


@pytest.mark.asyncio
async def test_cancel_terminates_in_flight_turn(tmp_path: Path) -> None:
    launcher = _make_launcher(tmp_path)
    script = _write_script(
        tmp_path / "hang.jsonl",
        [
            {"type": "system", "subtype": "init", "session_id": "cli-hang"},
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "partial"},
                },
            },
        ],
    )

    backend = ClaudeCodeBackend()
    cfg = BackendConfig(
        backend="claude_code",
        cwd=str(tmp_path),
        env={
            "CLAUDE_STUB_SCRIPT": str(script),
            # Hang after the partial-text line so cancel has work to do.
            "CLAUDE_STUB_HANG_AFTER_LINES": "2",
        },
        extra={"claude_cli": str(launcher)},
    )
    await backend.initialize("loom-cancel", cfg, capabilities={})
    stream = await backend.send_message("loom-cancel", "go")

    iterator = stream.__aiter__()
    # Consume up to the partial text so we know the stub is running.
    first = await iterator.__anext__()
    assert first.chunk_type == "text_delta"

    await backend.cancel("loom-cancel")

    # After cancel, the iterator should terminate within a few extra events.
    saw_terminator = False
    async for chunk in iterator:
        if chunk.chunk_type in ("end_turn", "error"):
            saw_terminator = True
            break
    assert saw_terminator, "cancelled stream did not surface a terminator chunk"

    await backend.close("loom-cancel")


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path: Path) -> None:
    launcher = _make_launcher(tmp_path)
    script = _write_script(
        tmp_path / "noop.jsonl",
        [
            {"type": "system", "subtype": "init", "session_id": "cli-noop"},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "stop_reason": "end_turn",
            },
        ],
    )

    backend = ClaudeCodeBackend()
    cfg = BackendConfig(
        backend="claude_code",
        cwd=str(tmp_path),
        env={"CLAUDE_STUB_SCRIPT": str(script)},
        extra={"claude_cli": str(launcher)},
    )
    await backend.initialize("loom-close", cfg, capabilities={})
    await _drain(await backend.send_message("loom-close", "hi"))

    await backend.close("loom-close")
    # A second close on an already-closed session must not raise.
    await backend.close("loom-close")


@pytest.mark.asyncio
async def test_handle_permission_raises_in_print_mode(tmp_path: Path) -> None:
    backend = ClaudeCodeBackend()
    request = PermissionRequest(
        session_id="loom-perm",
        tool_name="shell.run",
        arguments={"cmd": "ls"},
    )
    with pytest.raises(AdapterError):
        await backend.handle_permission(request, granted=True)


@pytest.mark.asyncio
async def test_resume_args_returns_empty_tuple() -> None:
    backend = ClaudeCodeBackend()
    assert backend.resume_args("/some/frozen.jsonl") == ()


@pytest.mark.asyncio
async def test_resolve_claude_cli_via_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Adapter must respect STRATOCLAVE_LOOM_CLAUDE_CLI when extra is empty."""
    launcher = _make_launcher(tmp_path)
    monkeypatch.setenv(ENV_CLAUDE_CLI, str(launcher))
    script = _write_script(
        tmp_path / "via-env.jsonl",
        [
            {"type": "system", "subtype": "init", "session_id": "via-env"},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "stop_reason": "end_turn",
            },
        ],
    )

    backend = ClaudeCodeBackend()
    cfg = BackendConfig(
        backend="claude_code",
        cwd=str(tmp_path),
        env={"CLAUDE_STUB_SCRIPT": str(script)},
        # No claude_cli in extra — must come from the env var.
        extra={},
    )
    await backend.initialize("loom-env", cfg, capabilities={})
    chunks = await _drain(await backend.send_message("loom-env", "hi"))
    await backend.close("loom-env")
    assert chunks[-1].chunk_type == "end_turn"


@pytest.mark.asyncio
async def test_tool_use_block_is_normalized_in_stream(tmp_path: Path) -> None:
    launcher = _make_launcher(tmp_path)
    script = _write_script(
        tmp_path / "tool.jsonl",
        [
            {"type": "system", "subtype": "init", "session_id": "tool-sess"},
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "tu_1",
                        "name": "Bash",
                        "input": {"cmd": "ls"},
                    },
                },
            },
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "stop_reason": "tool_use",
            },
        ],
    )

    backend = ClaudeCodeBackend()
    cfg = BackendConfig(
        backend="claude_code",
        cwd=str(tmp_path),
        env={"CLAUDE_STUB_SCRIPT": str(script)},
        extra={"claude_cli": str(launcher)},
    )
    await backend.initialize("loom-tool", cfg, capabilities={})
    chunks = await _drain(await backend.send_message("loom-tool", "go"))
    await backend.close("loom-tool")

    tool_chunks = [c for c in chunks if c.chunk_type == "tool_use"]
    assert len(tool_chunks) == 1
    assert tool_chunks[0].content["tool_name"] == "shell.run"
    assert tool_chunks[0].content["tool_native_name"] == "Bash"
    assert tool_chunks[0].content["tool_use_id"] == "tu_1"
    assert tool_chunks[0].content["input"] == {"cmd": "ls"}


@pytest.mark.asyncio
async def test_error_result_emits_error_chunk(tmp_path: Path) -> None:
    launcher = _make_launcher(tmp_path)
    script = _write_script(
        tmp_path / "err.jsonl",
        [
            {"type": "system", "subtype": "init", "session_id": "err-sess"},
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "api_error_status": "rate_limited",
            },
        ],
    )

    backend = ClaudeCodeBackend()
    cfg = BackendConfig(
        backend="claude_code",
        cwd=str(tmp_path),
        env={"CLAUDE_STUB_SCRIPT": str(script)},
        extra={"claude_cli": str(launcher)},
    )
    await backend.initialize("loom-err", cfg, capabilities={})
    chunks = await _drain(await backend.send_message("loom-err", "go"))
    await backend.close("loom-err")
    assert chunks[-1].chunk_type == "error"
    assert chunks[-1].content["reason"] == "rate_limited"
