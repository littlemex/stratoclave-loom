"""Core types and protocols for stratoclave-loom."""

from stratoclave_loom.core.backend import AgentBackend
from stratoclave_loom.core.errors import (
    AdapterError,
    BackendNotFoundError,
    LoomError,
    PermissionDeniedError,
    SessionClosedError,
    TransportError,
)
from stratoclave_loom.core.session import AgentSession
from stratoclave_loom.core.types import (
    AcpChunk,
    BackendConfig,
    NormalizedTurn,
    PermissionRequest,
)

__all__ = [
    "AcpChunk",
    "AdapterError",
    "AgentBackend",
    "AgentSession",
    "BackendConfig",
    "BackendNotFoundError",
    "LoomError",
    "NormalizedTurn",
    "PermissionDeniedError",
    "PermissionRequest",
    "SessionClosedError",
    "TransportError",
]
