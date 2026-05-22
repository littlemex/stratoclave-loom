"""Wire-level tests for KiroCodeBackend using a Python ACP stub server.

The stub speaks JSON-RPC 2.0 over stdio (initialize / session/new /
session/load / session/prompt / session/cancel) and replays a JSONL of
session/update notifications to drive a turn.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from stratoclave_loom.adapters.kiro_code import ENV_KIRO_CLI, KiroCodeBackend
from stratoclave_loom.core.errors import AdapterError
from stratoclave_loom.core.types import AcpChunk, BackendConfig, PermissionRequest

_STUB_PATH = Path(__file__).parent / "_kiro_stub.py"


def _make_launcher(tmp_path: Path) -> Path:
    """Create a bash shim that invokes the python stub.

    The adapter spawns ``<kiro_cli> acp ...``; the shim drops those flags
    and runs the stub directly. The stub keys all behaviour off env vars,
    so the dropped flags don't matter for these tests.
    """
    launcher = tmp_path / "kiro-shim"
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
async def test_initialize_and_text_turn(tmp_path: Path) -> None:
    launcher = _make_launcher(tmp_path)
    script = _write_script(
        tmp_path / "text.jsonl",
        [
            # Bare update objects — stub wraps them in session/update frames.
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Hel"}},
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "lo"}},
        ],
    )
    backend = KiroCodeBackend()
    cfg = BackendConfig(
        backend="kiro_code",
        cwd=str(tmp_path),
        env={
            "KIRO_STUB_SCRIPT": str(script),
            "KIRO_STUB_SESSION_ID": "sess-1",
        },
        extra={"kiro_cli": str(launcher), "trust_all_tools": True},
    )
    agreed = await backend.initialize("loom-text", cfg, capabilities={"foo": "bar"})
    assert agreed["foo"] == "bar"
    assert "agentInfo" in agreed

    chunks = await _drain(await backend.send_message("loom-text", "hi"))
    await backend.close("loom-text")

    types = [c.chunk_type for c in chunks]
    assert types == ["text_delta", "text_delta", "end_turn"]
    assert chunks[0].content["text"] == "Hel"
    assert chunks[1].content["text"] == "lo"
    assert chunks[2].content["stop_reason"] == "end_turn"
    assert [c.seq for c in chunks] == [0, 1, 2]


@pytest.mark.asyncio
async def test_tool_call_and_result_translation(tmp_path: Path) -> None:
    launcher = _make_launcher(tmp_path)
    script = _write_script(
        tmp_path / "tool.jsonl",
        [
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tu_1",
                "title": "Running: echo hi",
                "kind": "shell",
                "rawInput": {"command": "echo hi"},
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tu_1",
                "status": "completed",
                "rawOutput": {
                    "items": [{"Json": {"exit_status": "exit status: 0", "stdout": "hi\n"}}]
                },
            },
        ],
    )
    backend = KiroCodeBackend()
    cfg = BackendConfig(
        backend="kiro_code",
        cwd=str(tmp_path),
        env={"KIRO_STUB_SCRIPT": str(script)},
        extra={"kiro_cli": str(launcher), "trust_all_tools": True},
    )
    await backend.initialize("loom-tool", cfg, capabilities={})
    chunks = await _drain(await backend.send_message("loom-tool", "go"))
    await backend.close("loom-tool")

    tool_use = [c for c in chunks if c.chunk_type == "tool_use"]
    tool_result = [c for c in chunks if c.chunk_type == "tool_result"]
    assert len(tool_use) == 1
    assert tool_use[0].content["tool_name"] == "shell.run"
    assert tool_use[0].content["tool_native_name"] == "shell"
    assert tool_use[0].content["tool_use_id"] == "tu_1"
    assert tool_use[0].content["input"] == {"command": "echo hi"}
    assert len(tool_result) == 1
    assert tool_result[0].content["tool_use_id"] == "tu_1"
    assert tool_result[0].content["is_error"] is False
    assert chunks[-1].chunk_type == "end_turn"


@pytest.mark.asyncio
async def test_cancel_mid_turn_emits_cancelled_stop_reason(tmp_path: Path) -> None:
    launcher = _make_launcher(tmp_path)
    script = _write_script(
        tmp_path / "hang.jsonl",
        [
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "partial"},
            },
        ],
    )
    backend = KiroCodeBackend()
    cfg = BackendConfig(
        backend="kiro_code",
        cwd=str(tmp_path),
        env={
            "KIRO_STUB_SCRIPT": str(script),
            "KIRO_STUB_HANG_AFTER_LINES": "1",
        },
        extra={"kiro_cli": str(launcher), "trust_all_tools": True},
    )
    await backend.initialize("loom-cancel", cfg, capabilities={})
    stream = await backend.send_message("loom-cancel", "go")
    iterator = stream.__aiter__()
    first = await iterator.__anext__()
    assert first.chunk_type == "text_delta"

    await backend.cancel("loom-cancel")

    saw_terminator = False
    async for chunk in iterator:
        if chunk.chunk_type in ("end_turn", "error"):
            saw_terminator = True
            assert chunk.content.get("stop_reason") in ("cancelled", "end_turn")
            break
    assert saw_terminator
    await backend.close("loom-cancel")


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path: Path) -> None:
    launcher = _make_launcher(tmp_path)
    script = _write_script(tmp_path / "noop.jsonl", [])
    backend = KiroCodeBackend()
    cfg = BackendConfig(
        backend="kiro_code",
        cwd=str(tmp_path),
        env={"KIRO_STUB_SCRIPT": str(script)},
        extra={"kiro_cli": str(launcher), "trust_all_tools": True},
    )
    await backend.initialize("loom-close", cfg, capabilities={})
    await _drain(await backend.send_message("loom-close", "hi"))

    await backend.close("loom-close")
    # Second close on an already-closed session must not raise.
    await backend.close("loom-close")


@pytest.mark.asyncio
async def test_handle_permission_raises(tmp_path: Path) -> None:
    backend = KiroCodeBackend()
    request = PermissionRequest(
        session_id="loom-perm",
        tool_name="shell.run",
        arguments={"cmd": "ls"},
    )
    with pytest.raises(AdapterError):
        await backend.handle_permission(request, granted=True)


@pytest.mark.asyncio
async def test_resume_args_returns_empty_tuple() -> None:
    backend = KiroCodeBackend()
    assert backend.resume_args("/some/frozen.jsonl") == ()


@pytest.mark.asyncio
async def test_resolve_kiro_cli_via_env_var(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    launcher = _make_launcher(tmp_path)
    monkeypatch.setenv(ENV_KIRO_CLI, str(launcher))
    script = _write_script(tmp_path / "via-env.jsonl", [])
    backend = KiroCodeBackend()
    cfg = BackendConfig(
        backend="kiro_code",
        cwd=str(tmp_path),
        env={"KIRO_STUB_SCRIPT": str(script)},
        extra={"trust_all_tools": True},  # no kiro_cli — must come from env
    )
    await backend.initialize("loom-env", cfg, capabilities={})
    chunks = await _drain(await backend.send_message("loom-env", "hi"))
    await backend.close("loom-env")
    assert chunks[-1].chunk_type == "end_turn"


@pytest.mark.asyncio
async def test_resume_from_jsonl_is_rejected(tmp_path: Path) -> None:
    launcher = _make_launcher(tmp_path)
    backend = KiroCodeBackend()
    cfg = BackendConfig(
        backend="kiro_code",
        cwd=str(tmp_path),
        resume_from_jsonl=str(tmp_path / "any.jsonl"),
        extra={"kiro_cli": str(launcher)},
    )
    with pytest.raises(AdapterError):
        await backend.initialize("loom-resume", cfg, capabilities={})


@pytest.mark.asyncio
async def test_argv_carries_trust_tools_and_engine(tmp_path: Path) -> None:
    launcher = _make_launcher(tmp_path)
    argv_record = tmp_path / "argv.json"
    script = _write_script(tmp_path / "argv.jsonl", [])
    backend = KiroCodeBackend()
    cfg = BackendConfig(
        backend="kiro_code",
        cwd=str(tmp_path),
        env={
            "KIRO_STUB_SCRIPT": str(script),
            "KIRO_STUB_RECORD_ARGV": str(argv_record),
        },
        extra={
            "kiro_cli": str(launcher),
            "trust_tools": ("shell", "read"),
            "agent_engine": "v2",
            "model": "auto",
        },
    )
    await backend.initialize("loom-argv", cfg, capabilities={})
    await _drain(await backend.send_message("loom-argv", "hi"))
    await backend.close("loom-argv")

    argv = json.loads(argv_record.read_text())
    assert "acp" in argv
    assert "--trust-tools" in argv
    idx = argv.index("--trust-tools")
    assert argv[idx + 1] == "shell,read"
    assert "--agent-engine" in argv
    assert argv[argv.index("--agent-engine") + 1] == "v2"
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "auto"


@pytest.mark.asyncio
async def test_session_load_with_acp_session_id(tmp_path: Path) -> None:
    launcher = _make_launcher(tmp_path)
    script = _write_script(
        tmp_path / "loaded.jsonl",
        [{"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "ok"}}],
    )
    backend = KiroCodeBackend()
    cfg = BackendConfig(
        backend="kiro_code",
        cwd=str(tmp_path),
        env={
            "KIRO_STUB_SCRIPT": str(script),
            "KIRO_STUB_SESSION_ID": "should-not-be-used",
        },
        extra={
            "kiro_cli": str(launcher),
            "trust_all_tools": True,
            "acp_session_id": "preexisting-sess",
        },
    )
    await backend.initialize("loom-loaded", cfg, capabilities={})
    chunks = await _drain(await backend.send_message("loom-loaded", "hi"))
    await backend.close("loom-loaded")
    assert chunks[-1].chunk_type == "end_turn"
