"""High-level session wrapper exposed to library users."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import TracebackType
from typing import Any

from stratoclave_loom.core.backend import AgentBackend
from stratoclave_loom.core.errors import SessionClosedError
from stratoclave_loom.core.types import (
    AcpChunk,
    BackendConfig,
    ModelFilter,
    ModelInfo,
    PermissionRequest,
)


class AgentSession:
    """Owns a single agent subprocess for the lifetime of a session.

    Use :func:`stratoclave_loom.create_session` instead of constructing
    instances directly. Always use the session as an async context manager
    so that ``close`` is invoked on shutdown:

    .. code-block:: python

        async with create_session(cfg) as session:
            async for chunk in session.send_message("hello"):
                ...
    """

    def __init__(
        self,
        backend: AgentBackend,
        config: BackendConfig,
        *,
        session_id: str | None = None,
    ) -> None:
        self._backend = backend
        self._config = config
        self._session_id = session_id or str(uuid.uuid4())
        self._initialized = False
        self._closed = False

    @property
    def id(self) -> str:
        return self._session_id

    @property
    def backend_name(self) -> str:
        return self._backend.backend_name

    async def initialize(
        self,
        capabilities: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise SessionClosedError(f"session {self._session_id!r} already closed")
        agreed = await self._backend.initialize(self._session_id, self._config, capabilities or {})
        self._initialized = True
        return dict(agreed)

    async def send_message(
        self,
        content: str,
        *,
        context_files: tuple[str, ...] = (),
        model: str | None = None,
        history: tuple[Any, ...] | None = None,
    ) -> AsyncIterator[AcpChunk]:
        if self._closed:
            raise SessionClosedError(f"session {self._session_id!r} already closed")
        if not self._initialized:
            await self.initialize()
        # Each backend's ``send_message`` is itself an ``async def`` returning
        # an :class:`AsyncIterator`, so we must await it to obtain the iterator
        # before handing it back to the caller. ``model`` and ``history``
        # are opaque from the session's point of view -- adapters that
        # cannot route on them must ignore the parameters rather than
        # reject them.
        return await self._backend.send_message(
            self._session_id,
            content,
            context_files=context_files,
            model=model,
            history=history,
        )

    async def list_models(self, filter: ModelFilter | None = None) -> tuple[ModelInfo, ...]:
        """Forward the catalogue query to the wrapped backend."""

        return await self._backend.list_models(filter)

    @property
    def default_model_id(self) -> str | None:
        """Backend-suggested initial selection (``None`` means not applicable)."""

        return self._backend.default_model_id

    async def cancel(self) -> None:
        if self._closed:
            return
        await self._backend.cancel(self._session_id)

    async def respond_permission(
        self,
        request: PermissionRequest,
        granted: bool,
    ) -> None:
        if self._closed:
            raise SessionClosedError(f"session {self._session_id!r} already closed")
        await self._backend.handle_permission(request, granted)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._backend.close(self._session_id)

    async def __aenter__(self) -> AgentSession:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()
