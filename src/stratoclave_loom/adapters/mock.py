"""In-memory mock backend used by unit tests and the getting-started demo.

The mock does not spawn a subprocess. It echoes the user content back as a
sequence of ``text_delta`` chunks followed by an ``end_turn``. Optional
``extra`` configuration can be used to inject scripted chunks.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from stratoclave_loom.adapters._registry import register_backend
from stratoclave_loom.core.backend import AgentBackend
from stratoclave_loom.core.errors import AdapterError, SessionClosedError
from stratoclave_loom.core.types import (
    AcpChunk,
    BackendConfig,
    NormalizedTurn,
    PermissionRequest,
)


class MockBackend(AgentBackend):
    """A deterministic in-memory backend for tests and quickstarts."""

    backend_name = "mock"

    def __init__(self) -> None:
        self._configs: dict[str, BackendConfig] = {}
        self._cancelled: set[str] = set()

    async def initialize(
        self,
        session_id: str,
        config: BackendConfig,
        capabilities: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if config.resume_from_jsonl is not None:
            # The mock does not actually replay the file; it just records
            # that resume was requested so tests can assert the parameter
            # made it through.
            pass
        self._configs[session_id] = config
        return dict(capabilities)

    async def send_message(
        self,
        session_id: str,
        content: str,
        *,
        context_files: tuple[str, ...] = (),
    ) -> AsyncIterator[AcpChunk]:
        if session_id not in self._configs:
            raise SessionClosedError(f"session {session_id!r} is not active")
        return self._stream(session_id, content)

    async def _stream(self, session_id: str, content: str) -> AsyncIterator[AcpChunk]:
        # Yield each character as a text_delta to exercise streaming code.
        seq = 0
        for ch in content:
            if session_id in self._cancelled:
                self._cancelled.discard(session_id)
                yield AcpChunk(
                    session_id=session_id,
                    chunk_type="error",
                    content={"reason": "cancelled"},
                    seq=seq,
                )
                return
            yield AcpChunk(
                session_id=session_id,
                chunk_type="text_delta",
                content={"text": ch},
                seq=seq,
            )
            seq += 1
        yield AcpChunk(
            session_id=session_id,
            chunk_type="end_turn",
            content={},
            seq=seq,
        )

    async def cancel(self, session_id: str) -> None:
        self._cancelled.add(session_id)

    async def close(self, session_id: str) -> None:
        self._configs.pop(session_id, None)
        self._cancelled.discard(session_id)

    async def handle_permission(
        self,
        request: PermissionRequest,
        granted: bool,
    ) -> None:
        # Mock never requests permission; calling this is a programmer error.
        raise AdapterError("mock backend never issues permission requests; this call is unexpected")

    def normalize(self, raw_line: str, seq: int) -> list[NormalizedTurn]:
        # The mock does not produce JSONL output. Returning an empty list is
        # the contract for "nothing to ingest".
        return []

    def resume_args(self, frozen_jsonl_path: str) -> tuple[str, ...]:
        # No on-disk resume for the mock.
        return ()


register_backend("mock", MockBackend)
