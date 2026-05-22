"""Drive a real Claude Code turn through stratoclave-loom.

Usage::

    # Default: prefer claude_code, then kiro_code, fall back to mock.
    python examples/quickstart.py "say OK and nothing else"

    # Force a backend.
    python examples/quickstart.py --backend kiro_code "echo me"
    python examples/quickstart.py --backend mock "echo me"

The script prints each :class:`AcpChunk` on its own line as JSON so you can
pipe it into ``jq``. It exits non-zero if the turn ends in an error chunk.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path

# Importing the package registers all built-in backends as a side effect.
from stratoclave_loom import (
    AcpChunk,
    BackendConfig,
    create_session,
    list_backends,
)


def _pick_backend(requested: str | None) -> str:
    if requested is not None:
        return requested
    if shutil.which("claude") is not None:
        return "claude_code"
    if shutil.which("kiro-cli") is not None:
        return "kiro_code"
    return "mock"


def _default_extra(backend: str) -> dict[str, object]:
    """Backend-specific defaults that keep the demo non-interactive."""
    if backend == "kiro_code":
        # kiro_code's --print-mode equivalent does not have a runtime
        # permission callback; pre-trust all tools so a single-shot prompt
        # can finish without a TTY prompt.
        return {"trust_all_tools": True}
    return {}


async def _run(backend: str, prompt: str, cwd: str) -> int:
    config = BackendConfig(backend=backend, cwd=cwd, extra=_default_extra(backend))
    async with create_session(config) as session:
        stream = await session.send_message(prompt)
        last_chunk: AcpChunk | None = None
        async for chunk in stream:
            payload = {
                "session_id": chunk.session_id,
                "type": chunk.chunk_type,
                "seq": chunk.seq,
                "content": dict(chunk.content),
            }
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            last_chunk = chunk
        if last_chunk is not None and last_chunk.chunk_type == "error":
            return 1
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="stratoclave-loom quickstart")
    parser.add_argument(
        "prompt",
        nargs="?",
        default="reply with the single word OK and nothing else",
        help="Prompt to send to the agent.",
    )
    parser.add_argument(
        "--backend",
        choices=sorted(list_backends()),
        default=None,
        help="Backend name. Defaults to claude_code if available, else kiro_code, else mock.",
    )
    parser.add_argument(
        "--cwd",
        default=str(Path.cwd()),
        help="Working directory for the agent. Defaults to the current directory.",
    )
    args = parser.parse_args(argv)
    backend = _pick_backend(args.backend)
    sys.stderr.write(f"[loom] using backend: {backend}\n")
    return asyncio.run(_run(backend, args.prompt, args.cwd))


if __name__ == "__main__":
    raise SystemExit(main())
