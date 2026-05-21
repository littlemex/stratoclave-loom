"""Command-line entry point for stratoclave-loom.

Subcommands:

* ``list-backends`` — print the names of registered adapters.
* ``run`` — open a session against a backend, send a single message, and
  stream the response chunks to stdout as JSON Lines.

The CLI is intentionally minimal: it exists for diagnostics and the
getting-started demo, not as a production interface.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from stratoclave_loom import (
    BackendConfig,
    create_session,
    list_backends,
)


def _parse_kv(arg: str) -> tuple[str, str]:
    if "=" not in arg:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {arg!r}")
    key, _, value = arg.partition("=")
    if not key:
        raise argparse.ArgumentTypeError(f"empty key in {arg!r}")
    return key, value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stratoclave-loom",
        description="Spawn and inspect coding-agent backends via the loom abstraction.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "list-backends",
        help="List the names of all registered backends.",
    )

    run = sub.add_parser(
        "run",
        help="Send a single message to a backend and stream the response.",
    )
    run.add_argument(
        "--backend",
        required=True,
        help="Backend name (see `list-backends`).",
    )
    run.add_argument(
        "--message",
        required=True,
        help="User message to send.",
    )
    run.add_argument(
        "--cwd",
        default=".",
        help="Working directory passed to the backend (default: current dir).",
    )
    run.add_argument(
        "--env",
        action="append",
        type=_parse_kv,
        default=[],
        metavar="KEY=VALUE",
        help="Environment variable forwarded to the backend (repeatable).",
    )
    run.add_argument(
        "--allow-tool",
        action="append",
        default=None,
        metavar="TOOL",
        help="Restrict the agent to these normalized tool names (repeatable).",
    )
    run.add_argument(
        "--resume",
        default=None,
        metavar="PATH",
        help="Path to a frozen JSONL transcript to resume from.",
    )
    return parser


async def _run_subcommand(args: argparse.Namespace) -> int:
    cfg = BackendConfig(
        backend=args.backend,
        cwd=args.cwd,
        env=dict(args.env or {}),
        allowed_tools=tuple(args.allow_tool) if args.allow_tool else None,
        resume_from_jsonl=args.resume,
    )
    async with create_session(cfg) as session:
        stream = await session.send_message(args.message)
        async for chunk in stream:
            payload: dict[str, Any] = {
                "session_id": chunk.session_id,
                "chunk_type": chunk.chunk_type,
                "content": dict(chunk.content),
            }
            if chunk.agent_id is not None:
                payload["agent_id"] = chunk.agent_id
            if chunk.seq is not None:
                payload["seq"] = chunk.seq
            print(json.dumps(payload, ensure_ascii=False))
            if chunk.chunk_type in ("end_turn", "error"):
                break
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "list-backends":
        for name in list_backends():
            print(name)
        return 0

    if args.command == "run":
        try:
            return asyncio.run(_run_subcommand(args))
        except KeyboardInterrupt:
            print("interrupted", file=sys.stderr)
            return 130

    parser.error(f"unknown command: {args.command!r}")
    return 2  # pragma: no cover - argparse already exits


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
