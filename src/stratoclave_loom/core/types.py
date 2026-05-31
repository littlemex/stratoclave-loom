"""Core dataclasses exchanged across the stratoclave-loom API surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Self

ChunkType = Literal[
    "text_delta",
    "tool_use",
    "tool_result",
    "thought",
    "permission_request",
    "end_turn",
    "error",
]

Role = Literal["user", "assistant", "tool_use", "tool_result", "system"]


@dataclass(frozen=True, slots=True)
class AcpChunk:
    """A normalized streaming chunk emitted by an agent backend.

    The `content` field is intentionally a free-form mapping so that adapter
    authors can carry adapter-specific payloads, while still allowing
    application code to rely on the `chunk_type` discriminator and on the
    common keys documented per chunk type.
    """

    session_id: str
    chunk_type: ChunkType
    content: Mapping[str, Any]
    agent_id: str | None = None
    seq: int | None = None


@dataclass(frozen=True, slots=True)
class PermissionRequest:
    """Issued when an agent requests permission for a side-effecting tool call.

    Tool names are *normalized* (e.g. ``shell.run``, ``file.write``).
    Adapters are responsible for translating their backend-specific names
    into the common namespace.
    """

    session_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class NormalizedTurn:
    """A single normalized turn extracted from a backend's JSONL output.

    Carries enough information for higher layers (e.g. stratoclave-distill)
    to reason about a session without parsing backend-specific formats,
    while preserving the original line for re-normalization later.
    """

    turn_id: str
    session_id: str
    seq: int
    role: Role
    text_content: str
    tool_name: str | None
    tool_input: Mapping[str, Any] | None
    occurred_at: str
    raw_line: str


@dataclass(frozen=True, slots=True)
class BackendConfig:
    """Configuration for spawning a backend session.

    The fields are deliberately conservative: anything that could embed
    a hardcoded path, URL, or credential is exposed as configuration so
    that callers can route via stratoclave or any other proxy.
    """

    backend: str
    cwd: str
    env: Mapping[str, str] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] | None = None
    """If set, the adapter must restrict the agent to these *normalized* tool
    names. ``None`` means no restriction (adapter's default policy applies)."""

    resume_from_jsonl: str | None = None
    """Optional path to a frozen JSONL transcript used to resume an archived
    session. Adapters that do not support resume must raise
    :class:`stratoclave_loom.core.errors.AdapterError` if this is set."""

    extra: Mapping[str, Any] = field(default_factory=dict)
    """Adapter-specific options. Keys with no matching adapter must be
    ignored silently to allow forward-compatibility."""


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """A single selectable model exposed by an :class:`AgentBackend`.

    Adapters surface their model catalogue through
    :meth:`AgentBackend.list_models` so the host application can render
    a picker without having to know what a "Bedrock model id" or
    "Claude profile" looks like. The fields are deliberately generic:

    * ``id`` -- the opaque token the adapter expects back in
      ``send_message(..., model=id)``. Adapters must round-trip this
      value verbatim.
    * ``name`` -- short, human-readable label (``"Claude Opus 4.7"``).
    * ``family`` -- coarse grouping for filtering (``"claude"``,
      ``"nova"``, ``"llama"`` …). May be ``None`` when the adapter
      cannot infer a family.
    * ``provider`` -- upstream owner of the model (``"anthropic"``,
      ``"amazon"`` …). May be ``None`` for adapter-internal models.
    * ``description`` -- one-line free-text shown as a tooltip.
    * ``extra`` -- adapter-specific metadata (modality flags, region
      constraints …) that the picker UI may use opportunistically but
      must tolerate not understanding.

    The struct is hashable / frozen so callers can stash it in sets or
    use it as a dict key without worrying about mutation.
    """

    id: str
    name: str
    family: str | None = None
    provider: str | None = None
    description: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelFilter:
    """Reusable predicate for narrowing a noisy model list.

    Backends like Bedrock surface hundreds of foundation-model entries;
    a picker UI is unusable without a way to slice them. ``ModelFilter``
    lives in the core so every adapter (and any host application) can
    consume the same filter spec without re-implementing substring /
    family matching.

    Match semantics
    ---------------
    * ``substring`` is case-insensitive and tested against ``id`` *and*
      ``name`` *and* ``family`` joined by spaces; this lets
      ``"opus 4.7"`` find ``"Claude Opus 4.7"`` regardless of which
      field carries the version.
    * ``family`` is an exact case-insensitive equality against
      ``ModelInfo.family``.
    * ``provider`` is an exact case-insensitive equality against
      ``ModelInfo.provider``.

    All conditions are ANDed; a ``None`` field means "do not constrain
    on this axis". An empty filter (default constructor) matches every
    model and is the right value to pass when the caller has no
    preference.
    """

    substring: str | None = None
    family: str | None = None
    provider: str | None = None

    def matches(self, model: ModelInfo) -> bool:
        """Return ``True`` iff ``model`` satisfies every set condition."""

        if self.family is not None and (model.family or "").lower() != self.family.lower():
            return False
        if self.provider is not None and (model.provider or "").lower() != self.provider.lower():
            return False
        if self.substring:
            haystack = " ".join(
                part
                for part in (model.id, model.name, model.family or "", model.provider or "")
                if part
            ).lower()
            if self.substring.lower() not in haystack:
                return False
        return True

    def apply(self, models: tuple[ModelInfo, ...]) -> tuple[ModelInfo, ...]:
        """Return only the entries from ``models`` that match this filter."""

        return tuple(m for m in models if self.matches(m))

    @classmethod
    def empty(cls) -> Self:
        """An always-matching filter; equivalent to ``ModelFilter()``."""

        return cls()
