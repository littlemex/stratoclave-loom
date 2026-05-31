"""Kiro Code adapter — drives ``kiro-cli acp`` over ACP JSON-RPC.

Wire format (observed against ``kiro-cli`` 2.4.0)
-------------------------------------------------

Unlike Claude Code's ``--print`` mode, ``kiro-cli acp`` is a long-lived ACP
agent: one subprocess hosts many ``session/prompt`` turns. The framing is
JSON-RPC 2.0 over newline-delimited JSON on stdio.

Client → agent (requests)::

    {"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
    {"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"...","mcpServers":[]}}
    {"jsonrpc":"2.0","id":3,"method":"session/prompt",
     "params":{"sessionId":"<uuid>","prompt":[{"type":"text","text":"..."}]}}

Agent → client during a turn (notifications)::

    {"method":"session/update","params":{"sessionId":"...",
       "update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"..."}}}}
    {"method":"session/update","params":{"sessionId":"...",
       "update":{"sessionUpdate":"tool_call","toolCallId":"...","title":"...","kind":"execute","rawInput":{...}}}}
    {"method":"session/update","params":{"sessionId":"...",
       "update":{"sessionUpdate":"tool_call_update","toolCallId":"...","status":"completed","rawOutput":{...}}}}

Agent → client (request response)::

    {"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn"}}

Cancellation: send ``session/cancel`` as a notification with the sessionId.
The agent will resolve the in-flight ``session/prompt`` with
``stopReason:"cancelled"``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
from collections.abc import AsyncIterator, Mapping
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

#: Environment variable to override the ``kiro-cli`` executable.
ENV_KIRO_CLI = "STRATOCLAVE_LOOM_KIRO_CLI"

# Tool-name normalization map. kiro-cli's built-in tool names → loom's
# normalized namespace. Unknown names pass through unchanged.
_NORMALIZED_FROM_NATIVE: dict[str, str] = {
    "shell": "shell.run",
    "read": "file.read",
    "write": "file.write",
    "grep": "file.grep",
    "glob": "file.glob",
    "code": "code.intel",
    "use_aws": "aws.cli",
    "web_fetch": "web.fetch",
    "web_search": "web.search",
}


def _resolve_kiro_cli(extra: Mapping[str, Any]) -> str:
    """Find the ``kiro-cli`` executable.

    Resolution order: ``BackendConfig.extra['kiro_cli']`` →
    ``$STRATOCLAVE_LOOM_KIRO_CLI`` → ``shutil.which('kiro-cli')``.
    """
    candidate = extra.get("kiro_cli") if isinstance(extra, Mapping) else None
    if isinstance(candidate, str) and candidate:
        return candidate
    env_path = os.environ.get(ENV_KIRO_CLI)
    if env_path:
        return env_path
    found = shutil.which("kiro-cli")
    if found:
        return found
    raise AdapterError(
        "could not locate the 'kiro-cli' CLI; set BackendConfig.extra['kiro_cli'] "
        f"or {ENV_KIRO_CLI}"
    )


def _normalized_tool_name(native: str) -> str:
    return _NORMALIZED_FROM_NATIVE.get(native, native)


class _SessionState:
    """Per-session bookkeeping for one kiro_code session.

    A loom session owns a single ``kiro-cli acp`` subprocess and a
    persistent ACP session id minted via ``session/new``.
    """

    __slots__ = (
        "acp_session_id",
        "active",
        "config",
        "kiro_cli",
        "next_request_id",
        "pending_responses",
        "reader_task",
        "stream_queue",
        "transport",
        "turn_in_flight",
        "turn_request_id",
    )

    def __init__(self, config: BackendConfig, kiro_cli: str) -> None:
        self.config = config
        self.kiro_cli = kiro_cli
        self.transport: StdioTransport | None = None
        self.reader_task: asyncio.Task[None] | None = None
        self.pending_responses: dict[int, asyncio.Future[Mapping[str, Any]]] = {}
        # Notifications for the in-flight turn land on stream_queue. None is
        # the sentinel that marks "stream is finished" (e.g. on close).
        self.stream_queue: asyncio.Queue[Mapping[str, Any] | None] = asyncio.Queue()
        self.acp_session_id: str | None = None
        self.next_request_id = 1
        self.turn_in_flight = False
        self.turn_request_id: int | None = None
        self.active = True


class KiroCodeBackend(AgentBackend):
    """Kiro CLI adapter using ``kiro-cli acp`` (Agent Client Protocol)."""

    backend_name = "kiro_code"

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
            raise AdapterError(
                "kiro_code adapter does not support resume_from_jsonl; "
                "pass extra['acp_session_id'] to attach to an existing ACP session"
            )
        kiro_cli = _resolve_kiro_cli(config.extra)
        state = _SessionState(config, kiro_cli)

        argv = self._build_argv(state)
        env = self._build_env(config)
        transport = StdioTransport(argv=argv, cwd=config.cwd, env=env)
        await transport.start()
        state.transport = transport
        state.reader_task = asyncio.create_task(
            self._reader_loop(state),
            name=f"loom-kiro-reader-{session_id}",
        )

        try:
            init_result = await self._call(
                state,
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                    },
                },
            )

            # If the caller pre-supplied an ACP session id (extra['acp_session_id']),
            # use it directly via session/load. Otherwise mint a fresh one.
            extra = config.extra
            acp_session_id_hint: str | None = None
            if isinstance(extra, Mapping):
                hint = extra.get("acp_session_id")
                if isinstance(hint, str) and hint:
                    acp_session_id_hint = hint

            if acp_session_id_hint is not None:
                # session/load reattaches to an existing ACP session. The agent
                # may stream replay updates; we ignore them here because no
                # send_message has been issued yet (queue is drained in _stream).
                await self._call(
                    state,
                    "session/load",
                    {"sessionId": acp_session_id_hint, "cwd": config.cwd, "mcpServers": []},
                )
                state.acp_session_id = acp_session_id_hint
            else:
                new_result = await self._call(
                    state,
                    "session/new",
                    {"cwd": config.cwd, "mcpServers": list(self._mcp_servers(config))},
                )
                sid = new_result.get("sessionId")
                if not isinstance(sid, str):
                    raise AdapterError(
                        f"kiro-cli returned a session/new result without sessionId: {new_result!r}"
                    )
                state.acp_session_id = sid
        except Exception:
            await self._tear_down(state)
            raise

        self._sessions[session_id] = state
        # Surface the agreed agent capabilities so callers can introspect.
        agreed = dict(capabilities)
        if isinstance(init_result, Mapping):
            agreed["agentCapabilities"] = init_result.get("agentCapabilities", {})
            agreed["agentInfo"] = init_result.get("agentInfo", {})
        return agreed

    async def send_message(
        self,
        session_id: str,
        content: str,
        *,
        context_files: tuple[str, ...] = (),
        model: str | None = None,
        history: tuple[Mapping[str, Any], ...] | None = None,
    ) -> AsyncIterator[AcpChunk]:
        # Kiro Code does not expose a runtime model picker via ACP and
        # owns its own conversation state; accept ``model`` / ``history``
        # for protocol compatibility and drop them.
        del model, history
        state = self._sessions.get(session_id)
        if state is None or not state.active:
            raise SessionClosedError(f"session {session_id!r} is not active")
        if state.turn_in_flight:
            raise AdapterError(
                f"session {session_id!r} already has a turn in flight; "
                "await the previous send_message stream before issuing another"
            )
        if state.acp_session_id is None:
            raise AdapterError("ACP session id is not set; initialize() must complete first")

        # Build the prompt blocks: leading context_files become resource_link
        # blocks (best-effort; agents that ignore them are still given the text).
        prompt_blocks: list[Mapping[str, Any]] = []
        for path in context_files:
            prompt_blocks.append({"type": "resource_link", "uri": f"file://{path}", "name": path})
        prompt_blocks.append({"type": "text", "text": content})

        request_id = state.next_request_id
        state.next_request_id += 1
        state.turn_in_flight = True
        state.turn_request_id = request_id
        future: asyncio.Future[Mapping[str, Any]] = asyncio.get_event_loop().create_future()
        state.pending_responses[request_id] = future

        # Drain any stale notifications buffered before this turn.
        while not state.stream_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                state.stream_queue.get_nowait()

        await self._send_raw(
            state,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": state.acp_session_id,
                    "prompt": prompt_blocks,
                },
            },
        )

        return self._stream(session_id, state, request_id, future)

    async def cancel(self, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state is None or not state.turn_in_flight or state.transport is None:
            return
        with contextlib.suppress(TransportError):
            await self._send_raw(
                state,
                {
                    "jsonrpc": "2.0",
                    "method": "session/cancel",
                    "params": {"sessionId": state.acp_session_id},
                },
            )

    async def close(self, session_id: str) -> None:
        state = self._sessions.pop(session_id, None)
        if state is None:
            return
        await self._tear_down(state)

    async def handle_permission(
        self,
        request: PermissionRequest,
        granted: bool,
    ) -> None:
        # The current loom adapter spawns kiro-cli with --trust-all-tools so
        # the agent never asks for runtime permission. Until we wire the ACP
        # session/request_permission round-trip into AcpChunk, raising is
        # safer than silently accepting.
        raise AdapterError(
            "kiro_code adapter does not yet route runtime permission decisions; "
            "spawn with extra['trust_all_tools']=True or extra['trust_tools']=[...] "
            "to pre-approve at session start"
        )

    # ---- argv / env -----------------------------------------------------

    def _build_argv(self, state: _SessionState) -> tuple[str, ...]:
        argv: list[str] = [state.kiro_cli, "acp"]
        extra = state.config.extra if isinstance(state.config.extra, Mapping) else {}

        agent = extra.get("agent")
        if isinstance(agent, str) and agent:
            argv.extend(["--agent", agent])
        model = extra.get("model")
        if isinstance(model, str) and model:
            argv.extend(["--model", model])

        # Trust policy: prefer trust_tools (allowlist) over trust_all_tools.
        trust_tools = extra.get("trust_tools")
        if isinstance(trust_tools, list | tuple) and trust_tools:
            native = ",".join(str(t) for t in trust_tools if isinstance(t, str))
            argv.extend(["--trust-tools", native])
        elif extra.get("trust_all_tools") is True:
            argv.append("--trust-all-tools")
        elif state.config.allowed_tools is not None:
            # Fall back to the loom-standard allowlist; translate normalized
            # names back to kiro's native names.
            native = ",".join(_native_tool_name(name) for name in state.config.allowed_tools)
            if native:
                argv.extend(["--trust-tools", native])

        engine = extra.get("agent_engine")
        if isinstance(engine, str) and engine in ("v1", "v2", "kas"):
            argv.extend(["--agent-engine", engine])

        token_path = extra.get("token_path")
        if isinstance(token_path, str) and token_path:
            argv.extend(["--token-path", token_path])

        extra_argv = extra.get("extra_cli_args")
        if isinstance(extra_argv, list | tuple):
            for a in extra_argv:
                if isinstance(a, str):
                    argv.append(a)
        return tuple(argv)

    def _build_env(self, config: BackendConfig) -> dict[str, str]:
        env = os.environ.copy()
        for key, value in config.env.items():
            env[key] = value
        return env

    def _mcp_servers(self, config: BackendConfig) -> tuple[Mapping[str, Any], ...]:
        extra = config.extra if isinstance(config.extra, Mapping) else {}
        servers = extra.get("mcp_servers")
        if isinstance(servers, list | tuple):
            return tuple(s for s in servers if isinstance(s, Mapping))
        return ()

    # ---- JSON-RPC plumbing ---------------------------------------------

    async def _send_raw(self, state: _SessionState, payload: Mapping[str, Any]) -> None:
        if state.transport is None:
            raise TransportError("transport is not active")
        await state.transport.send(payload)

    async def _call(
        self,
        state: _SessionState,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout_s: float = 30.0,
    ) -> Mapping[str, Any]:
        request_id = state.next_request_id
        state.next_request_id += 1
        future: asyncio.Future[Mapping[str, Any]] = asyncio.get_event_loop().create_future()
        state.pending_responses[request_id] = future
        await self._send_raw(
            state,
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)},
        )
        try:
            async with asyncio.timeout(timeout_s):
                return await future
        finally:
            state.pending_responses.pop(request_id, None)

    async def _reader_loop(self, state: _SessionState) -> None:
        """Demultiplex JSON-RPC frames from the agent's stdout."""
        transport = state.transport
        if transport is None:
            return
        try:
            async for msg in transport.receive():
                # Response to a previous request.
                if "id" in msg and ("result" in msg or "error" in msg):
                    rid = msg.get("id")
                    if isinstance(rid, int) and rid in state.pending_responses:
                        future = state.pending_responses.pop(rid)
                        if "error" in msg and not future.done():
                            err = msg["error"]
                            future.set_exception(
                                AdapterError(f"kiro-cli {msg.get('method', '?')} error: {err!r}")
                            )
                        elif not future.done():
                            result = msg.get("result")
                            future.set_result(result if isinstance(result, Mapping) else {})
                        # If this is the in-flight turn's terminal response,
                        # signal the stream to drain.
                        if rid == state.turn_request_id:
                            await state.stream_queue.put(msg)
                    continue

                # Notifications: only forward those that belong to a turn.
                method = msg.get("method")
                if isinstance(method, str) and method == "session/update" and state.turn_in_flight:
                    await state.stream_queue.put(msg)
                # _kiro.dev/* and other unknown notifications are ignored.
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - reader resilience
            # Surface the failure to the in-flight turn (if any) so it can
            # exit instead of hanging forever.
            if state.turn_in_flight:
                with contextlib.suppress(Exception):
                    await state.stream_queue.put({"_loom_reader_error": repr(exc)})
        finally:
            # Wake any waiters: pending requests fail; in-flight turn ends.
            for rid, fut in list(state.pending_responses.items()):
                if not fut.done():
                    fut.set_exception(TransportError("kiro-cli stream ended"))
                state.pending_responses.pop(rid, None)
            with contextlib.suppress(Exception):
                await state.stream_queue.put(None)

    async def _stream(
        self,
        loom_session_id: str,
        state: _SessionState,
        request_id: int,
        prompt_future: asyncio.Future[Mapping[str, Any]],
    ) -> AsyncIterator[AcpChunk]:
        seq = 0
        try:
            while True:
                msg = await state.stream_queue.get()
                if msg is None:
                    # Reader closed. If the prompt future already has a
                    # terminator we wouldn't be here; surface as an error.
                    yield AcpChunk(
                        session_id=loom_session_id,
                        chunk_type="error",
                        content={"reason": "stream_closed"},
                        seq=seq,
                    )
                    return
                if "_loom_reader_error" in msg:
                    yield AcpChunk(
                        session_id=loom_session_id,
                        chunk_type="error",
                        content={"reason": "reader_failure", "detail": msg["_loom_reader_error"]},
                        seq=seq,
                    )
                    return

                # Terminal: response to our session/prompt request.
                if msg.get("id") == request_id and ("result" in msg or "error" in msg):
                    if "error" in msg:
                        yield AcpChunk(
                            session_id=loom_session_id,
                            chunk_type="error",
                            content={"reason": "session_prompt_error", "error": msg["error"]},
                            seq=seq,
                        )
                    else:
                        result = msg.get("result") or {}
                        yield AcpChunk(
                            session_id=loom_session_id,
                            chunk_type="end_turn",
                            content={
                                "stop_reason": result.get("stopReason"),
                            },
                            seq=seq,
                        )
                    return

                # Notification: translate to zero-or-more chunks.
                for chunk in self._translate(msg, loom_session_id):
                    stamped = AcpChunk(
                        session_id=chunk.session_id,
                        chunk_type=chunk.chunk_type,
                        content=chunk.content,
                        agent_id=chunk.agent_id,
                        seq=seq,
                    )
                    seq += 1
                    yield stamped
        finally:
            state.turn_in_flight = False
            state.turn_request_id = None
            if not prompt_future.done():
                # The reader will clean this up; nothing else to do.
                pass

    def _translate(
        self,
        msg: Mapping[str, Any],
        out_session_id: str,
    ) -> list[AcpChunk]:
        if msg.get("method") != "session/update":
            return []
        params = msg.get("params")
        if not isinstance(params, Mapping):
            return []
        update = params.get("update")
        if not isinstance(update, Mapping):
            return []
        kind = update.get("sessionUpdate")

        if kind == "agent_message_chunk":
            content = update.get("content")
            if isinstance(content, Mapping) and content.get("type") == "text":
                return [
                    AcpChunk(
                        session_id=out_session_id,
                        chunk_type="text_delta",
                        content={"text": str(content.get("text", ""))},
                    )
                ]
            return []

        if kind == "agent_thought_chunk":
            content = update.get("content")
            if isinstance(content, Mapping) and content.get("type") == "text":
                return [
                    AcpChunk(
                        session_id=out_session_id,
                        chunk_type="thought",
                        content={"text": str(content.get("text", ""))},
                    )
                ]
            return []

        if kind == "tool_call":
            return [
                AcpChunk(
                    session_id=out_session_id,
                    chunk_type="tool_use",
                    content={
                        "phase": "start",
                        "tool_use_id": update.get("toolCallId"),
                        "tool_name": _normalized_tool_name(str(update.get("kind", ""))),
                        "tool_native_name": update.get("kind"),
                        "title": update.get("title"),
                        "input": update.get("rawInput", {}),
                    },
                )
            ]

        if kind == "tool_call_update":
            status = update.get("status")
            if status in ("completed", "failed"):
                return [
                    AcpChunk(
                        session_id=out_session_id,
                        chunk_type="tool_result",
                        content={
                            "tool_use_id": update.get("toolCallId"),
                            "is_error": status == "failed",
                            "content": update.get("content") or update.get("rawOutput"),
                        },
                    )
                ]
            # In-progress streaming output — surface as a tool_use delta.
            return [
                AcpChunk(
                    session_id=out_session_id,
                    chunk_type="tool_use",
                    content={
                        "phase": "update",
                        "tool_use_id": update.get("toolCallId"),
                        "content": update.get("content"),
                    },
                )
            ]

        return []

    # ---- teardown -------------------------------------------------------

    async def _tear_down(self, state: _SessionState) -> None:
        state.active = False
        state.turn_in_flight = False
        if state.reader_task is not None:
            state.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await state.reader_task
            state.reader_task = None
        if state.transport is not None:
            with contextlib.suppress(Exception):
                await state.transport.close()
            state.transport = None

    # ---- normalization (replay) ----------------------------------------

    def normalize(self, raw_line: str, seq: int) -> list[NormalizedTurn]:
        """Best-effort normalization of an ACP JSONL transcript line.

        We accept either a raw ``session/update`` notification (as captured
        from the wire) or a higher-level ``{role, content}`` shape so that
        callers can re-use the same recorder for kiro and claude.
        """
        obj = decode_jsonl_line(raw_line)
        if obj is None:
            return []

        # Wire-shape: a session/update notification.
        if obj.get("method") == "session/update":
            params = obj.get("params")
            if not isinstance(params, Mapping):
                return []
            update = params.get("update")
            if not isinstance(update, Mapping):
                return []
            kind = update.get("sessionUpdate")
            session_id = str(params.get("sessionId", ""))
            occurred_at = str(obj.get("timestamp") or "")
            turn_id = f"turn-{seq}"

            if kind == "agent_message_chunk":
                content = update.get("content")
                if isinstance(content, Mapping) and content.get("type") == "text":
                    return [
                        NormalizedTurn(
                            turn_id=turn_id,
                            session_id=session_id,
                            seq=seq,
                            role="assistant",
                            text_content=str(content.get("text", "")),
                            tool_name=None,
                            tool_input=None,
                            occurred_at=occurred_at,
                            raw_line=raw_line,
                        )
                    ]
            if kind == "tool_call":
                native = str(update.get("kind", ""))
                return [
                    NormalizedTurn(
                        turn_id=turn_id,
                        session_id=session_id,
                        seq=seq,
                        role="tool_use",
                        text_content="",
                        tool_name=_normalized_tool_name(native),
                        tool_input=update.get("rawInput")
                        if isinstance(update.get("rawInput"), Mapping)
                        else None,
                        occurred_at=occurred_at,
                        raw_line=raw_line,
                    )
                ]
            return []

        # Higher-level shape mirroring claude_code.normalize.
        message = obj.get("message")
        if isinstance(message, Mapping):
            role = message.get("role")
            if role in ("user", "assistant"):
                session_id = str(obj.get("sessionId") or obj.get("session_id") or "")
                occurred_at = str(obj.get("timestamp") or "")
                turn_id = str(obj.get("uuid") or obj.get("id") or f"turn-{seq}")
                text = message.get("content")
                if isinstance(text, str):
                    return [
                        NormalizedTurn(
                            turn_id=turn_id,
                            session_id=session_id,
                            seq=seq,
                            role=role,
                            text_content=text,
                            tool_name=None,
                            tool_input=None,
                            occurred_at=occurred_at,
                            raw_line=raw_line,
                        )
                    ]
        return []

    def resume_args(self, frozen_jsonl_path: str) -> tuple[str, ...]:
        # kiro-cli does not currently expose a JSONL-replay flag. Callers
        # with a frozen ACP session id should pass extra['acp_session_id']
        # to initialize() instead.
        return ()


def _native_tool_name(normalized: str) -> str:
    for native, mapped in _NORMALIZED_FROM_NATIVE.items():
        if mapped == normalized:
            return native
    return normalized


register_backend("kiro_code", KiroCodeBackend)
