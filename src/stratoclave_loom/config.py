"""Library-wide configuration loaded from environment variables.

stratoclave-loom never embeds paths, model names, or URLs. Anything that
might vary across deployments is exposed either through :class:`BackendConfig`
or through environment variables read by this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LoomSettings:
    """Process-wide tunables read once at import time.

    Use :func:`get_settings` to retrieve the cached instance. Re-import is
    required if you change environment variables after import.
    """

    log_level: str
    """Library log level. Values: ``DEBUG``/``INFO``/``WARNING``/``ERROR``."""

    buffer_bytes: int
    """Maximum bytes the stdio transport buffers before applying back-pressure."""

    cancel_grace_ms: int
    """Milliseconds between SIGINT and SIGTERM during cancellation."""


_DEFAULT_LOG_LEVEL = "INFO"
_DEFAULT_BUFFER_BYTES = 64 * 1024
_DEFAULT_CANCEL_GRACE_MS = 500


def _read_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"environment variable {name}={raw!r} is not a valid integer") from exc


def _read_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw


def load_settings() -> LoomSettings:
    """Read the current environment and produce a :class:`LoomSettings`."""
    return LoomSettings(
        log_level=_read_str("STRATOCLAVE_LOOM_LOG_LEVEL", _DEFAULT_LOG_LEVEL).upper(),
        buffer_bytes=_read_int("STRATOCLAVE_LOOM_BUFFER", _DEFAULT_BUFFER_BYTES),
        cancel_grace_ms=_read_int("STRATOCLAVE_LOOM_CANCEL_GRACE_MS", _DEFAULT_CANCEL_GRACE_MS),
    )


_cached: LoomSettings | None = None


def get_settings() -> LoomSettings:
    """Return a process-wide cached :class:`LoomSettings`."""
    global _cached
    if _cached is None:
        _cached = load_settings()
    return _cached


def reset_settings_cache() -> None:
    """Reset the cached settings. Intended for tests."""
    global _cached
    _cached = None
