"""Error hierarchy for stratoclave-loom."""

from __future__ import annotations


class LoomError(Exception):
    """Base class for all stratoclave-loom errors."""


class BackendNotFoundError(LoomError):
    """Raised when a requested backend name is not registered."""


class TransportError(LoomError):
    """Raised when the underlying transport (stdio, RPC, ...) misbehaves."""


class AdapterError(LoomError):
    """Raised by adapters when they cannot satisfy a request.

    Examples: an unsupported feature (``resume_from_jsonl``), a malformed
    response from the wrapped CLI, or a cancellation propagation failure.
    """


class SessionClosedError(LoomError):
    """Raised when an operation is attempted on an already-closed session."""


class PermissionDeniedError(LoomError):
    """Raised when a tool call is blocked by ``allowed_tools`` policy."""
