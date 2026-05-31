"""stratoclave-loom: a Pure Python abstraction over coding agent CLIs.

Public API. See :mod:`stratoclave_loom.core` for the underlying types and
:mod:`stratoclave_loom.adapters` for built-in backends.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

# Importing the adapters package registers the built-in backends.
from stratoclave_loom import adapters as _adapters  # noqa: F401
from stratoclave_loom.adapters._registry import (
    get_backend,
    list_backends,
    register_backend,
)
from stratoclave_loom.core import (
    AcpChunk,
    AdapterError,
    AgentBackend,
    AgentSession,
    BackendConfig,
    BackendNotFoundError,
    LoomError,
    ModelFilter,
    ModelInfo,
    NormalizedTurn,
    PermissionDeniedError,
    PermissionRequest,
    SessionClosedError,
    TransportError,
)

try:
    __version__ = version("stratoclave-loom")
except PackageNotFoundError:  # pragma: no cover - source checkouts
    __version__ = "0.0.0+unknown"


def create_session(
    config: BackendConfig,
    *,
    session_id: str | None = None,
) -> AgentSession:
    """Resolve a backend by name and wrap it in an :class:`AgentSession`.

    The returned session has not yet contacted the backend; the first
    ``send_message`` (or an explicit ``initialize``) triggers initialization.
    """
    backend = get_backend(config.backend)
    return AgentSession(backend, config, session_id=session_id)


__all__ = [
    "AcpChunk",
    "AdapterError",
    "AgentBackend",
    "AgentSession",
    "BackendConfig",
    "BackendNotFoundError",
    "LoomError",
    "ModelFilter",
    "ModelInfo",
    "NormalizedTurn",
    "PermissionDeniedError",
    "PermissionRequest",
    "SessionClosedError",
    "TransportError",
    "__version__",
    "create_session",
    "get_backend",
    "list_backends",
    "register_backend",
]
