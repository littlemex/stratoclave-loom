"""Claude Code adapter (skeleton — full ACP wiring lands later in v0.1).

The Claude Code CLI does not yet expose a stable ACP server. The intended
strategy is hybrid:

1. Spawn ``claude`` in headless mode using ``StdioTransport``.
2. Translate user messages into the CLI's input format and read structured
   JSONL chunks from stdout.
3. Map each chunk to :class:`AcpChunk` via tool-name normalization.

This module currently registers the ``claude_code`` name and validates
configuration so that callers fail loudly until the wire-level work lands.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from stratoclave_loom.adapters._base import decode_jsonl_line
from stratoclave_loom.adapters._registry import register_backend
from stratoclave_loom.core.backend import AgentBackend
from stratoclave_loom.core.errors import AdapterError
from stratoclave_loom.core.types import (
    AcpChunk,
    BackendConfig,
    NormalizedTurn,
    PermissionRequest,
)

# Tool-name normalization map. See docs/DESIGN.md §5.6.
_NORMALIZED_FROM_NATIVE: dict[str, str] = {
    "Bash": "shell.run",
    "Read": "file.read",
    "Write": "file.write",
    "Edit": "file.edit",
    "Glob": "file.glob",
    "Grep": "file.grep",
}


class ClaudeCodeBackend(AgentBackend):
    """Skeleton Claude Code adapter.

    The full implementation will spawn the ``claude`` CLI via
    :class:`stratoclave_loom.transport.stdio.StdioTransport`. For now this
    class only implements the parts that have no external dependency:
    JSONL normalization and the resume_args contract.
    """

    backend_name = "claude_code"

    async def initialize(
        self,
        session_id: str,
        config: BackendConfig,
        capabilities: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        # The full implementation will negotiate streaming + tools here.
        # We at least validate that resume_from_jsonl points to an existing
        # path before the user invests time spawning a session.
        if config.resume_from_jsonl is not None:
            import asyncio
            from pathlib import Path

            p = Path(config.resume_from_jsonl)
            exists = await asyncio.to_thread(p.is_file)
            if not exists:
                raise AdapterError(f"resume_from_jsonl does not exist or is not a file: {p}")
        return dict(capabilities)

    async def send_message(
        self,
        session_id: str,
        content: str,
        *,
        context_files: tuple[str, ...] = (),
    ) -> AsyncIterator[AcpChunk]:
        raise NotImplementedError(
            "ClaudeCodeBackend.send_message is pending v0.1 wire-level work; "
            "use the 'mock' backend in the meantime."
        )

    async def cancel(self, session_id: str) -> None:
        raise NotImplementedError("ClaudeCodeBackend.cancel is pending v0.1 wire-level work.")

    async def close(self, session_id: str) -> None:
        # Idempotent no-op until the full impl owns subprocess lifecycle.
        return None

    async def handle_permission(
        self,
        request: PermissionRequest,
        granted: bool,
    ) -> None:
        raise NotImplementedError(
            "ClaudeCodeBackend.handle_permission is pending v0.1 wire-level work."
        )

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
        # Claude Code resumes by session id rather than by JSONL path. The
        # caller is expected to map a frozen JSONL to its session_id and
        # pass it via ``BackendConfig.extra``. Returning an empty tuple
        # signals "no positional CLI args"; the actual hookup will move to
        # ``BackendConfig.extra`` once the wire-level adapter is finished.
        return ()


register_backend("claude_code", ClaudeCodeBackend)
