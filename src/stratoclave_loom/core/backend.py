"""The :class:`AgentBackend` ABC defining the adapter contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from typing import Any

from stratoclave_loom.core.types import (
    AcpChunk,
    BackendConfig,
    ModelFilter,
    ModelInfo,
    NormalizedTurn,
    PermissionRequest,
)


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
        model: str | None = None,
        history: tuple[Mapping[str, Any], ...] | None = None,
    ) -> AsyncIterator[AcpChunk]:
        """Send a user message and return a stream of response chunks.

        Implementations are ``async def`` methods that return an
        :class:`AsyncIterator`. The returned iterator should yield until
        an ``end_turn`` chunk (or an ``error`` chunk) is emitted.

        The two-step shape (await the call, then iterate the result) lets
        adapters perform any pre-stream setup — e.g., dispatching to a
        subprocess — without forcing every caller to dance around the
        generator's first ``__anext__``.

        ``model`` is an opaque token from :meth:`list_models` selecting
        which underlying model the adapter should use for this turn.
        Adapters that cannot switch models at runtime (e.g. CLI-backed
        adapters where the model is baked into the binary) must accept
        the parameter and ignore it. Passing ``None`` means "stay on
        whatever the adapter last used for this session, falling back
        to :attr:`default_model_id`".

        ``history``, when supplied, is the prior turn list the host
        wants this stateless adapter to replay before the new
        ``content``. Each entry is a mapping with at least ``role``
        (``"user"`` / ``"assistant"``) and ``content`` (string).
        Stateless adapters like ``bedrock`` use this to reconstruct
        the conversation without keeping their own buffer across
        turns -- the host's event log becomes the single source of
        truth, which means a forked session inherits its parent's
        context for free. CLI-backed adapters that already maintain
        conversation state must accept the parameter and ignore it.
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

    # -- model picker surface (optional, default no-op) ---------------------
    #
    # Adapters that expose a runtime-selectable model catalogue override
    # both ``list_models`` and ``default_model_id``. The defaults below
    # keep CLI-backed adapters (claude_code / kiro_code) honest without
    # forcing every implementation to spell out an empty list.

    async def list_models(self, filter: ModelFilter | None = None) -> tuple[ModelInfo, ...]:
        """Return the catalogue of models the picker can offer.

        The default returns ``()``; CLI adapters whose model is baked
        in at install time leave this alone. Adapters that talk to a
        provider with a discoverable catalogue (Bedrock, vLLM, ...)
        override this to fetch the live list and apply ``filter``.

        ``filter`` lets callers narrow noisy catalogues without each
        adapter having to roll its own substring matcher; see
        :class:`ModelFilter` for the shared semantics.
        """

        del filter  # default has nothing to filter
        return ()

    @property
    def default_model_id(self) -> str | None:
        """Adapter-suggested initial selection.

        Returned to ``send_message`` when the caller passes ``model=None``
        and the session has not yet picked a model. ``None`` means "no
        notion of a model" -- the picker UI should hide its model
        selector for this backend.
        """

        return None
