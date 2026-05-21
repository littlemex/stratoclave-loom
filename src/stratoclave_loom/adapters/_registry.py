"""In-memory registry mapping backend names to factory callables."""

from __future__ import annotations

from collections.abc import Callable

from stratoclave_loom.core.backend import AgentBackend
from stratoclave_loom.core.errors import BackendNotFoundError

BackendFactory = Callable[[], AgentBackend]

_backends: dict[str, BackendFactory] = {}


def register_backend(name: str, factory: BackendFactory) -> None:
    """Register an adapter under ``name``.

    Re-registering a name overwrites the previous factory; this is useful
    for tests that swap in fakes but should be avoided in production code.
    """
    _backends[name] = factory


def get_backend(name: str) -> AgentBackend:
    """Resolve a backend instance by name."""
    try:
        factory = _backends[name]
    except KeyError as exc:
        raise BackendNotFoundError(
            f"backend {name!r} is not registered. Available: {sorted(_backends.keys())}"
        ) from exc
    return factory()


def list_backends() -> tuple[str, ...]:
    """Return the sorted tuple of registered backend names."""
    return tuple(sorted(_backends.keys()))
