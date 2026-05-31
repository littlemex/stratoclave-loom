"""Built-in tool implementations for the Bedrock adapter.

Bedrock's Converse API ships a ``toolConfig`` surface but does not run
the tools itself: the model emits ``toolUse`` blocks and the host is
expected to execute them and return ``toolResult`` blocks on the next
turn. Other adapters (claude_code, kiro_code) hand that responsibility
to the underlying CLI; for the native Bedrock backend we have to be the
runtime ourselves.

Scope
-----
The first cut ships two narrowly scoped tools that the user repeatedly
asked for:

* ``web_fetch`` -- HTTP GET against an absolute URL, body truncated to a
  hard cap, no redirects across hosts.
* ``file_read`` -- read a UTF-8 text file confined to the configured
  ``BackendConfig.cwd``. Symlink escapes are blocked.

Both tools are *read-only* by design. We deliberately do not expose
shell, write, or anything that mutates state in this initial pass; that
is what claude_code / kiro_code are for. The goal here is "Bedrock can
look something up" rather than "Bedrock is a coding agent".

Hard limits
-----------
The defaults below are picked to make a runaway model expensive to abuse
even before the host adds quotas:

* ``MAX_FETCH_BYTES`` -- 256 KiB; HTML / JSON snippets fit comfortably,
  but a 50 MB binary is rejected.
* ``MAX_READ_BYTES``  -- 256 KiB per file read.
* ``FETCH_TIMEOUT_SECONDS`` -- 15 s; conservative for "look this up"
  work and short enough that a hung host does not stall the chat.
* ``MAX_TOOL_CALLS_PER_TURN`` -- 5; caps how many ``toolUse`` blocks
  the adapter will service before forcing the model to summarise.

All limits are also overridable via ``BackendConfig.extra`` so a
deployment that needs more headroom can opt in explicitly rather than
relying on adapter defaults.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


MAX_FETCH_BYTES_DEFAULT = 256 * 1024
MAX_READ_BYTES_DEFAULT = 256 * 1024
FETCH_TIMEOUT_DEFAULT = 15.0
MAX_TOOL_CALLS_PER_TURN_DEFAULT = 5

# Adapter ``extra`` keys (matching the snake_case used by the rest of
# BedrockBackend.config.extra).
EXTRA_TOOLS_ENABLED = "tools_enabled"
EXTRA_MAX_FETCH_BYTES = "tool_max_fetch_bytes"
EXTRA_MAX_READ_BYTES = "tool_max_read_bytes"
EXTRA_FETCH_TIMEOUT = "tool_fetch_timeout_seconds"
EXTRA_MAX_TOOL_CALLS = "tool_max_calls_per_turn"


@dataclass(frozen=True, slots=True)
class ToolLimits:
    """Resolved per-session tool execution limits.

    Built once at session warmup so the streaming loop does not have to
    re-parse ``extra`` for each ``toolUse`` block.
    """

    enabled: bool
    max_fetch_bytes: int
    max_read_bytes: int
    fetch_timeout_seconds: float
    max_tool_calls_per_turn: int

    @classmethod
    def from_extra(cls, extra: Mapping[str, Any]) -> ToolLimits:
        return cls(
            enabled=bool(extra.get(EXTRA_TOOLS_ENABLED, True)),
            max_fetch_bytes=_int_or(extra.get(EXTRA_MAX_FETCH_BYTES), MAX_FETCH_BYTES_DEFAULT),
            max_read_bytes=_int_or(extra.get(EXTRA_MAX_READ_BYTES), MAX_READ_BYTES_DEFAULT),
            fetch_timeout_seconds=_float_or(extra.get(EXTRA_FETCH_TIMEOUT), FETCH_TIMEOUT_DEFAULT),
            max_tool_calls_per_turn=_int_or(
                extra.get(EXTRA_MAX_TOOL_CALLS), MAX_TOOL_CALLS_PER_TURN_DEFAULT
            ),
        )


def _int_or(value: Any, fallback: int) -> int:
    try:
        if value is None:
            return fallback
        out = int(value)
        return out if out > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def _float_or(value: Any, fallback: float) -> float:
    try:
        if value is None:
            return fallback
        out = float(value)
        return out if out > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def build_tool_config(limits: ToolLimits) -> dict[str, Any] | None:
    """Bedrock ``toolConfig`` payload describing the built-in tools.

    Returns ``None`` when tools are disabled so the caller can skip the
    parameter entirely.
    """

    if not limits.enabled:
        return None
    return {
        "tools": [
            {
                "toolSpec": {
                    "name": "web_fetch",
                    "description": (
                        "Fetch the body of an HTTP(S) URL and return up to "
                        f"{limits.max_fetch_bytes} bytes of UTF-8 text. "
                        "Use only for absolute URLs; do not pass credentials."
                    ),
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "url": {
                                    "type": "string",
                                    "description": ("Absolute http:// or https:// URL to fetch."),
                                }
                            },
                            "required": ["url"],
                        }
                    },
                }
            },
            {
                "toolSpec": {
                    "name": "file_read",
                    "description": (
                        "Read a UTF-8 text file from the working directory. "
                        f"Returns at most {limits.max_read_bytes} bytes. "
                        "Paths must resolve inside the configured cwd; "
                        "symlinks that escape the cwd are rejected."
                    ),
                    "inputSchema": {
                        "json": {
                            "type": "object",
                            "properties": {
                                "path": {
                                    "type": "string",
                                    "description": (
                                        "Path relative to the working "
                                        "directory, or an absolute path "
                                        "inside it."
                                    ),
                                }
                            },
                            "required": ["path"],
                        }
                    },
                }
            },
        ],
        "toolChoice": {"auto": {}},
    }


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def execute_tool(
    name: str,
    arguments: Mapping[str, Any],
    *,
    cwd: str,
    limits: ToolLimits,
) -> dict[str, Any]:
    """Dispatch a tool name to its handler and shape the response block.

    Returns a Bedrock ``toolResult`` content shape ready to be embedded
    in the next turn's ``messages``: ``{"text": str, "json": {...}}``
    plus a ``status`` flag. The host need not introspect the payload; it
    just round-trips the dict back through ``converse_stream``.
    """

    try:
        if name == "web_fetch":
            return _tool_web_fetch(arguments, limits=limits)
        if name == "file_read":
            return _tool_file_read(arguments, cwd=cwd, limits=limits)
        return _err(f"unknown tool: {name!r}")
    except Exception as exc:  # pragma: no cover - defence in depth
        logger.exception("tool %s raised", name)
        return _err(f"{name} failed: {exc}")


def _err(message: str) -> dict[str, Any]:
    return {"status": "error", "content": [{"text": message}]}


def _ok(text: str) -> dict[str, Any]:
    return {"status": "success", "content": [{"text": text}]}


def _tool_web_fetch(arguments: Mapping[str, Any], *, limits: ToolLimits) -> dict[str, Any]:
    url = str(arguments.get("url", "")).strip()
    if not url:
        return _err("missing 'url' argument")

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return _err(f"unsupported URL scheme: {parsed.scheme!r}; expected http/https")
    if not parsed.netloc:
        return _err("URL must be absolute (missing host)")

    # Stdlib over requests so we have one fewer optional dep. We use a
    # short timeout and cap the read size to ``max_fetch_bytes`` so a
    # large response does not run the host out of memory.
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "stratoclave-loom-bedrock/1.0 (+https://github.com/littlemex/stratoclave-loom)",
            "Accept": "text/html,application/json,text/plain;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=limits.fetch_timeout_seconds) as resp:
            status = resp.status
            content_type = resp.headers.get("Content-Type", "")
            # Read up to the cap + 1 byte so we can flag truncation.
            body = resp.read(limits.max_fetch_bytes + 1)
    except Exception as exc:
        return _err(f"web_fetch error: {exc}")

    truncated = len(body) > limits.max_fetch_bytes
    if truncated:
        body = body[: limits.max_fetch_bytes]

    # Decode best-effort. ``replace`` keeps malformed bytes from crashing
    # the loop while still leaving a visible marker for the model.
    text = body.decode("utf-8", errors="replace")
    header = f"HTTP {status} ({content_type or 'unknown content-type'}), {len(body)} bytes" + (
        " (truncated)" if truncated else ""
    )
    return _ok(f"{header}\n\n{text}")


def _tool_file_read(
    arguments: Mapping[str, Any], *, cwd: str, limits: ToolLimits
) -> dict[str, Any]:
    path_str = str(arguments.get("path", "")).strip()
    if not path_str:
        return _err("missing 'path' argument")

    base = Path(cwd).resolve()
    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = base / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError:
        return _err(f"file not found: {path_str}")
    except OSError as exc:
        return _err(f"file_read error: {exc}")

    # ``Path.is_relative_to`` (3.9+) -- pin the resolved path inside the
    # configured cwd so symlink escapes ("../../etc/passwd") fail loudly
    # rather than leaking host secrets.
    try:
        resolved.relative_to(base)
    except ValueError:
        return _err(f"path escapes the configured cwd: {path_str!r} resolves to {resolved}")
    if not resolved.is_file():
        return _err(f"not a regular file: {path_str}")

    try:
        size = resolved.stat().st_size
    except OSError as exc:
        return _err(f"file_read error: {exc}")
    truncated = size > limits.max_read_bytes
    try:
        with open(resolved, "rb") as handle:
            data = handle.read(limits.max_read_bytes)
    except OSError as exc:
        return _err(f"file_read error: {exc}")
    text = data.decode("utf-8", errors="replace")
    header = (
        f"{resolved.relative_to(base) if resolved != base else resolved.name} "
        f"({size} bytes" + (", truncated" if truncated else "") + ")"
    )
    return _ok(f"{header}\n\n{text}")


__all__ = [
    "EXTRA_TOOLS_ENABLED",
    "FETCH_TIMEOUT_DEFAULT",
    "MAX_FETCH_BYTES_DEFAULT",
    "MAX_READ_BYTES_DEFAULT",
    "MAX_TOOL_CALLS_PER_TURN_DEFAULT",
    "ToolLimits",
    "build_tool_config",
    "execute_tool",
]
