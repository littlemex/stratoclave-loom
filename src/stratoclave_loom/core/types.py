"""Core dataclasses exchanged across the stratoclave-loom API surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

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
