"""The :class:`AgentBackend` ABC defining the adapter contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from typing import Any

from stratoclave_loom.core.types import AcpChunk, BackendConfig, NormalizedTurn, PermissionRequest


class AgentBackend(ABC):
    """Adapter contract for a single coding-agent CLI.

    Implementations are expected to be **stateless across sessions**: each
    :class:`stratoclave_loom.core.session.AgentSession` owns its own
    subprocess and per-session state. The backend instance is shared across
    sessions and only carries adapter-wide configuration.
    """

    #: Stable identifier registered in :mod:`stratoclave_loom.adapters`.
    backend_name: str

    @abstractmethod
    async def initialize(
        self,
        session_id: str,
        config: BackendConfig,
        capabilities: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Negotiate capabilities for a new session.

        Returns the *agreed* capability set after negotiation. Adapters that
        do not support negotiation may simply return ``capabilities``.
        """

    @abstractmethod
    async def send_message(
        self,
        session_id: str,
        content: str,
        *,
        context_files: tuple[str, ...] = (),
    ) -> AsyncIterator[AcpChunk]:
        """Send a user message and return a stream of response chunks.

        Implementations are ``async def`` methods that return an
        :class:`AsyncIterator`. The returned iterator should yield until
        an ``end_turn`` chunk (or an ``error`` chunk) is emitted.

        The two-step shape (await the call, then iterate the result) lets
        adapters perform any pre-stream setup — e.g., dispatching to a
        subprocess — without forcing every caller to dance around the
        generator's first ``__anext__``.
        """

    @abstractmethod
    async def cancel(self, session_id: str) -> None:
        """Cancel any in-flight work for ``session_id``.

        Should be idempotent and safe to call after the session has ended.
        """

    @abstractmethod
    async def close(self, session_id: str) -> None:
        """Tear down resources for ``session_id``.

        After ``close``, no further calls except another ``close`` should be
        made for the given session.
        """

    @abstractmethod
    async def handle_permission(
        self,
        request: PermissionRequest,
        granted: bool,
    ) -> None:
        """Forward the user's permission decision back to the agent."""

    @abstractmethod
    def normalize(self, raw_line: str, seq: int) -> list[NormalizedTurn]:
        """Convert a single raw JSONL line into normalized turns.

        Returning more than one element is allowed for adapters whose JSONL
        encodes multiple semantic turns per line.
        """

    @abstractmethod
    def resume_args(self, frozen_jsonl_path: str) -> tuple[str, ...]:
        """Return CLI arguments needed to resume from a frozen transcript.

        Implementations that do not support resume must return ``()``.
        Higher layers must check the empty case before invoking the agent.
        """
