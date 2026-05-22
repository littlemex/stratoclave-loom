"""Claude Code adapter — spawns the ``claude`` CLI in headless streaming mode.

Wire format
-----------

Input (one JSON object per line on stdin)::

    {"type": "user", "message": {"role": "user", "content": "<text>"}}

Output (one JSON object per line on stdout, with --include-partial-messages)::

    {"type": "system", "subtype": "init", "session_id": "<uuid>", ...}
    {"type": "stream_event", "event": {"type": "content_block_delta",
                                       "delta": {"type": "text_delta",
                                                 "text": "..."}}}
    {"type": "stream_event", "event": {"type": "content_block_start",
                                       "content_block": {"type": "tool_use",
                                                         "name": "Bash",
                                                         "input": {...}}}}
    {"type": "assistant", "message": {...}}        # assembled (we skip; deltas already emitted)
    {"type": "result", "subtype": "success", "is_error": false, ...}   # terminal

The CLI's ``--print`` mode is single-turn. To support multi-turn within one
:class:`AgentSession`, we capture ``session_id`` from the first ``system/init``
and pass ``--resume <id>`` on subsequent ``send_message`` calls. Each
``send_message`` therefore owns one subprocess; ``cancel`` and ``close``
operate on the in-flight transport, if any.

Headless permissions are configured at spawn time via ``--allowed-tools`` /
``--permission-mode``. The interactive permission-callback dance has no
analog in ``--print`` mode, so :meth:`handle_permission` raises
:class:`AdapterError`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

from stratoclave_loom.adapters._base import decode_jsonl_line
from stratoclave_loom.adapters._registry import register_backend
from stratoclave_loom.core.backend import AgentBackend
from stratoclave_loom.core.errors import AdapterError, SessionClosedError, TransportError
from stratoclave_loom.core.types import (
    AcpChunk,
    BackendConfig,
    NormalizedTurn,
    PermissionRequest,
)
from stratoclave_loom.transport.stdio import StdioTransport

# Tool-name normalization map. See docs/DESIGN.md §5.6.
_NORMALIZED_FROM_NATIVE: dict[str, str] = {
    "Bash": "shell.run",
    "Read": "file.read",
    "Write": "file.write",
    "Edit": "file.edit",
    "Glob": "file.glob",
    "Grep": "file.grep",
}

#: Environment variable to override the ``claude`` executable used by the adapter.
ENV_CLAUDE_CLI = "STRATOCLAVE_LOOM_CLAUDE_CLI"


def _resolve_claude_cli(extra: Mapping[str, Any]) -> str:
    """Find the ``claude`` executable.

    Resolution order: ``BackendConfig.extra['claude_cli']`` →
    ``$STRATOCLAVE_LOOM_CLAUDE_CLI`` → ``shutil.which('claude')``.
    """
    candidate = extra.get("claude_cli") if isinstance(extra, Mapping) else None
    if isinstance(candidate, str) and candidate:
        return candidate
    env_path = os.environ.get(ENV_CLAUDE_CLI)
    if env_path:
        return env_path
    found = shutil.which("claude")
    if found:
        return found
    raise AdapterError(
        "could not locate the 'claude' CLI; set BackendConfig.extra['claude_cli'] "
        f"or {ENV_CLAUDE_CLI}"
    )


class _SessionState:
    """Per-session bookkeeping owned by the adapter."""

    __slots__ = ("active", "claude_cli", "cli_session_id", "config", "transport")

    def __init__(self, config: BackendConfig, claude_cli: str) -> None:
        self.config = config
        self.claude_cli = claude_cli
        # The CLI assigns its own session_id on the first turn; we capture it
        # from the system/init line and pass it via --resume on later turns.
        self.cli_session_id: str | None = None
        self.transport: StdioTransport | None = None
        self.active = True


class ClaudeCodeBackend(AgentBackend):
    """Claude Code adapter using ``--print --output-format stream-json``."""

    backend_name = "claude_code"

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionState] = {}

    # ---- lifecycle ------------------------------------------------------

    async def initialize(
        self,
        session_id: str,
        config: BackendConfig,
        capabilities: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if config.resume_from_jsonl is not None:
            p = Path(config.resume_from_jsonl)
            exists = await asyncio.to_thread(p.is_file)
            if not exists:
                raise AdapterError(f"resume_from_jsonl does not exist or is not a file: {p}")
        claude_cli = _resolve_claude_cli(config.extra)
        # Pre-seed the resume id from extra so callers can attach to a known
        # CLI session without going through a JSONL replay.
        state = _SessionState(config, claude_cli)
        extra_resume = (
            config.extra.get("cli_session_id") if isinstance(config.extra, Mapping) else None
        )
        if isinstance(extra_resume, str) and extra_resume:
            state.cli_session_id = extra_resume
        self._sessions[session_id] = state
        return dict(capabilities)

    async def send_message(
        self,
        session_id: str,
        content: str,
        *,
        context_files: tuple[str, ...] = (),
    ) -> AsyncIterator[AcpChunk]:
        state = self._sessions.get(session_id)
        if state is None or not state.active:
            raise SessionClosedError(f"session {session_id!r} is not active")
        if state.transport is not None:
            raise AdapterError(
                f"session {session_id!r} already has a turn in flight; "
                "await the previous send_message stream before issuing another"
            )
        argv = self._build_argv(state)
        env = self._build_env(state.config)
        transport = StdioTransport(argv=argv, cwd=state.config.cwd, env=env)
        await transport.start()
        state.transport = transport

        # Pre-write the user message and close stdin so the CLI knows the
        # input is complete. (--print + --input-format stream-json reads
        # until EOF on stdin.)
        try:
            await transport.send({"type": "user", "message": {"role": "user", "content": content}})
            await transport.close_stdin()
        except TransportError:
            await self._tear_down_transport(state)
            raise

        return self._stream(state, transport)

    async def cancel(self, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state is None or state.transport is None:
            return
        await state.transport.cancel()

    async def close(self, session_id: str) -> None:
        state = self._sessions.pop(session_id, None)
        if state is None:
            return
        state.active = False
        if state.transport is not None:
            await state.transport.close()
            state.transport = None

    async def handle_permission(
        self,
        request: PermissionRequest,
        granted: bool,
    ) -> None:
        # Claude Code's --print mode does not support runtime permission
        # callbacks; permissions must be configured at spawn via
        # --allowed-tools / --permission-mode. Surfacing this loudly is
        # better than silently no-oping a security-relevant decision.
        raise AdapterError(
            "claude_code adapter does not support runtime permission decisions in --print mode; "
            "configure BackendConfig.allowed_tools / extra['permission_mode'] at session start"
        )

    # ---- helpers --------------------------------------------------------

    def _build_argv(self, state: _SessionState) -> tuple[str, ...]:
        argv: list[str] = [
            state.claude_cli,
            "--print",
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--include-partial-messages",
            "--verbose",
        ]
        if state.cli_session_id is not None:
            argv.extend(["--resume", state.cli_session_id])
        if state.config.allowed_tools is not None:
            # Translate normalized names back to the CLI's native names. The
            # CLI accepts a comma- or space-separated list; we use commas to
            # avoid argv-splitting surprises.
            native = ",".join(_native_tool_name(name) for name in state.config.allowed_tools)
            argv.extend(["--allowed-tools", native])
        extra = state.config.extra
        if isinstance(extra, Mapping):
            permission_mode = extra.get("permission_mode")
            if isinstance(permission_mode, str) and permission_mode:
                argv.extend(["--permission-mode", permission_mode])
            extra_argv = extra.get("extra_cli_args")
            if isinstance(extra_argv, list | tuple):
                for a in extra_argv:
                    if isinstance(a, str):
                        argv.append(a)
        return tuple(argv)

    def _build_env(self, config: BackendConfig) -> dict[str, str]:
        # Inherit the parent environment so PATH / HOME / proxy vars are
        # available, then apply caller-supplied overrides last.
        env = os.environ.copy()
        for key, value in config.env.items():
            env[key] = value
        return env

    async def _tear_down_transport(self, state: _SessionState) -> None:
        import contextlib

        transport = state.transport
        if transport is None:
            return
        state.transport = None
        with contextlib.suppress(Exception):  # pragma: no cover - shutdown best-effort
            await transport.close()

    async def _stream(
        self,
        state: _SessionState,
        transport: StdioTransport,
    ) -> AsyncIterator[AcpChunk]:
        seq = 0
        # Reuse the AgentSession-level id for chunk routing; the CLI's own
        # id is captured separately so we can resume next turn.
        out_session_id = next(
            (sid for sid, s in self._sessions.items() if s is state),
            "",
        )
        try:
            async for msg in transport.receive():
                for chunk in self._translate(msg, out_session_id):
                    stamped = AcpChunk(
                        session_id=chunk.session_id,
                        chunk_type=chunk.chunk_type,
                        content=chunk.content,
                        agent_id=chunk.agent_id,
                        seq=seq,
                    )
                    seq += 1
                    yield stamped
                    if stamped.chunk_type in ("end_turn", "error"):
                        return
            # Stream ended without a result line — surface as an error so
            # callers do not silently see a truncated turn.
            yield AcpChunk(
                session_id=out_session_id,
                chunk_type="error",
                content={
                    "reason": "stream_ended_without_result",
                    "stderr": list(transport.stderr_log())[-20:],
                },
                seq=seq,
            )
        finally:
            await self._tear_down_transport(state)

    def _translate(
        self,
        msg: Mapping[str, Any],
        out_session_id: str,
    ) -> list[AcpChunk]:
        """Map one CLI stream-json line into zero or more :class:`AcpChunk`."""
        mtype = msg.get("type")
        chunks: list[AcpChunk] = []

        if mtype == "system":
            subtype = msg.get("subtype")
            if subtype == "init":
                # Capture CLI session_id for resume on next turn.
                cli_sid = msg.get("session_id")
                if isinstance(cli_sid, str):
                    state = self._sessions.get(out_session_id)
                    if state is not None:
                        state.cli_session_id = cli_sid
            return chunks

        if mtype == "stream_event":
            event = msg.get("event")
            if not isinstance(event, dict):
                return chunks
            etype = event.get("type")
            if etype == "content_block_delta":
                delta = event.get("delta")
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text = delta.get("text", "")
                    chunks.append(
                        AcpChunk(
                            session_id=out_session_id,
                            chunk_type="text_delta",
                            content={"text": str(text)},
                        )
                    )
                elif isinstance(delta, dict) and delta.get("type") == "input_json_delta":
                    # Partial tool-input json — surface as tool_use deltas so
                    # callers can stream a tool call as it is being built.
                    chunks.append(
                        AcpChunk(
                            session_id=out_session_id,
                            chunk_type="tool_use",
                            content={
                                "phase": "input_delta",
                                "partial_json": str(delta.get("partial_json", "")),
                            },
                        )
                    )
            elif etype == "content_block_start":
                block = event.get("content_block")
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    native_name = str(block.get("name", ""))
                    chunks.append(
                        AcpChunk(
                            session_id=out_session_id,
                            chunk_type="tool_use",
                            content={
                                "phase": "start",
                                "tool_name": _NORMALIZED_FROM_NATIVE.get(native_name, native_name),
                                "tool_native_name": native_name,
                                "tool_use_id": block.get("id"),
                                "input": block.get("input", {}),
                            },
                        )
                    )
            return chunks

        if mtype == "user":
            # Tool results arrive as user-role messages with tool_result blocks.
            message = msg.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            chunks.append(
                                AcpChunk(
                                    session_id=out_session_id,
                                    chunk_type="tool_result",
                                    content={
                                        "tool_use_id": block.get("tool_use_id"),
                                        "is_error": bool(block.get("is_error", False)),
                                        "content": block.get("content"),
                                    },
                                )
                            )
            return chunks

        if mtype == "assistant":
            # The assembled assistant message — we already emitted deltas, so
            # skip unless it carries something new (e.g. a thinking block).
            message = msg.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "thinking":
                            chunks.append(
                                AcpChunk(
                                    session_id=out_session_id,
                                    chunk_type="thought",
                                    content={"text": str(block.get("thinking", ""))},
                                )
                            )
            return chunks

        if mtype == "result":
            is_error = bool(msg.get("is_error", False))
            if is_error:
                chunks.append(
                    AcpChunk(
                        session_id=out_session_id,
                        chunk_type="error",
                        content={
                            "subtype": msg.get("subtype"),
                            "reason": msg.get("api_error_status") or msg.get("subtype"),
                        },
                    )
                )
            else:
                chunks.append(
                    AcpChunk(
                        session_id=out_session_id,
                        chunk_type="end_turn",
                        content={
                            "stop_reason": msg.get("stop_reason"),
                            "duration_ms": msg.get("duration_ms"),
                            "total_cost_usd": msg.get("total_cost_usd"),
                            "permission_denials": msg.get("permission_denials") or [],
                        },
                    )
                )
            return chunks

        return chunks

    # ---- normalization (for replayed JSONL transcripts) ----------------

    def normalize(self, raw_line: str, seq: int) -> list[NormalizedTurn]:
        """Normalize one Claude Code JSONL line into zero or more turns.

        Claude Code emits a single JSON object per line. The ``message`` key
        carries either a user or assistant message; ``content`` is a list of
        blocks (text or tool_use). Each block becomes one
        :class:`NormalizedTurn` so that higher layers can index them.
        """
        obj = decode_jsonl_line(raw_line)
        if obj is None:
            return []
        message = obj.get("message")
        if not isinstance(message, dict):
            return []
        role = message.get("role")
        if role not in ("user", "assistant"):
            return []
        session_id = str(obj.get("sessionId") or obj.get("session_id") or "")
        occurred_at = str(obj.get("timestamp") or obj.get("created_at") or "")
        turn_id_root = str(obj.get("uuid") or obj.get("id") or f"turn-{seq}")
        blocks = message.get("content")
        results: list[NormalizedTurn] = []

        if isinstance(blocks, str):
            results.append(
                NormalizedTurn(
                    turn_id=turn_id_root,
                    session_id=session_id,
                    seq=seq,
                    role=role,
                    text_content=blocks,
                    tool_name=None,
                    tool_input=None,
                    occurred_at=occurred_at,
                    raw_line=raw_line,
                )
            )
            return results

        if isinstance(blocks, list):
            for index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    results.append(
                        NormalizedTurn(
                            turn_id=f"{turn_id_root}#{index}",
                            session_id=session_id,
                            seq=seq,
                            role=role,
                            text_content=str(block.get("text", "")),
                            tool_name=None,
                            tool_input=None,
                            occurred_at=occurred_at,
                            raw_line=raw_line,
                        )
                    )
                elif btype == "tool_use":
                    native_name = str(block.get("name", ""))
                    normalized = _NORMALIZED_FROM_NATIVE.get(native_name, native_name)
                    results.append(
                        NormalizedTurn(
                            turn_id=f"{turn_id_root}#{index}",
                            session_id=session_id,
                            seq=seq,
                            role="tool_use",
                            text_content="",
                            tool_name=normalized,
                            tool_input=block.get("input"),
                            occurred_at=occurred_at,
                            raw_line=raw_line,
                        )
                    )
        return results

    def resume_args(self, frozen_jsonl_path: str) -> tuple[str, ...]:
        # Claude Code resumes by session id rather than by JSONL path. Callers
        # that have a frozen JSONL must extract the trailing session_id and
        # pass it via ``BackendConfig.extra['cli_session_id']``.
        return ()


def _native_tool_name(normalized: str) -> str:
    """Inverse of ``_NORMALIZED_FROM_NATIVE`` with passthrough for unknown names."""
    for native, mapped in _NORMALIZED_FROM_NATIVE.items():
        if mapped == normalized:
            return native
    return normalized


register_backend("claude_code", ClaudeCodeBackend)
