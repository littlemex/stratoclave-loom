"""Standalone stub used in place of the real ``kiro-cli acp`` server.

The stub speaks just enough JSON-RPC to satisfy KiroCodeBackend:

- ``initialize`` → returns a fixed agentCapabilities envelope.
- ``session/new`` → mints ``sess-1`` (or whatever ``KIRO_STUB_SESSION_ID`` is).
- ``session/load`` → accepts the supplied id and replies ``{}``.
- ``session/prompt`` → replays a JSONL of session/update notifications, then
  responds with ``{stopReason: <KIRO_STUB_STOP_REASON | end_turn>}``.
- ``session/cancel`` notification → marks the next prompt as cancelled.

Environment variables
---------------------

KIRO_STUB_SCRIPT
    Path to a JSONL file. Each line is emitted as a notification on stdout
    in order while a ``session/prompt`` is in flight. Each line must already
    be a valid JSON-RPC notification frame (i.e. include ``jsonrpc`` /
    ``method`` / ``params``) **or** a bare ACP ``update`` object that the
    stub will wrap in ``session/update``.

KIRO_STUB_SESSION_ID
    Optional. Overrides the sessionId returned by ``session/new``.

KIRO_STUB_STOP_REASON
    Optional. Overrides the stopReason returned by ``session/prompt``.

KIRO_STUB_DELAY_MS
    Optional. Sleep between notification lines.

KIRO_STUB_HANG_AFTER_LINES
    Optional. After emitting N notification lines, sleep forever — used to
    exercise cancellation. The stub will short-circuit and return a
    ``stopReason: cancelled`` once it observes a ``session/cancel``.

KIRO_STUB_RECORD_ARGV
    Optional. Path to write argv as JSON.

The stub never imports stratoclave_loom on purpose to keep startup minimal.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import sys
import time


def _read_one_line(timeout_s: float = 0.1) -> str | None:
    ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
    if not ready:
        return None
    line = sys.stdin.readline()
    if not line:
        return ""
    return line


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _load_script(path: str) -> list[dict]:
    if not path or not os.path.isfile(path):
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


def main() -> int:
    argv_path = os.environ.get("KIRO_STUB_RECORD_ARGV")
    if argv_path:
        with open(argv_path, "w", encoding="utf-8") as fh:
            json.dump(sys.argv, fh)

    session_id = os.environ.get("KIRO_STUB_SESSION_ID", "sess-1")
    stop_reason = os.environ.get("KIRO_STUB_STOP_REASON", "end_turn")
    delay_ms = int(os.environ.get("KIRO_STUB_DELAY_MS", "0"))
    hang_after = os.environ.get("KIRO_STUB_HANG_AFTER_LINES")
    hang_after_n = int(hang_after) if hang_after else None
    script_path = os.environ.get("KIRO_STUB_SCRIPT", "")
    script = _load_script(script_path)

    cancel_pending = False

    while True:
        line = _read_one_line(timeout_s=0.5)
        if line == "":  # EOF
            return 0
        if line is None:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        rid = req.get("id")

        if method == "initialize":
            _emit(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {
                            "loadSession": True,
                            "promptCapabilities": {
                                "image": True,
                                "audio": False,
                                "embeddedContext": False,
                            },
                            "mcpCapabilities": {"http": True, "sse": False},
                            "sessionCapabilities": {},
                        },
                        "authMethods": [],
                        "agentInfo": {"name": "kiro-stub", "version": "0.0"},
                    },
                }
            )
            continue

        if method == "session/new":
            _emit(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "sessionId": session_id,
                        "modes": {"currentModeId": "default", "availableModes": []},
                        "models": {"currentModelId": "auto", "availableModels": []},
                    },
                }
            )
            continue

        if method == "session/load":
            _emit({"jsonrpc": "2.0", "id": rid, "result": {}})
            continue

        if method == "session/cancel":
            # Notification — set a flag for the in-flight prompt.
            cancel_pending = True
            continue

        if method == "session/prompt":
            # Stream the script, watching for cancel between lines.
            for index, obj in enumerate(script):
                if cancel_pending:
                    break
                if "method" in obj and "params" in obj:
                    _emit(obj)
                else:
                    _emit(
                        {
                            "jsonrpc": "2.0",
                            "method": "session/update",
                            "params": {"sessionId": session_id, "update": obj},
                        }
                    )
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000.0)
                if hang_after_n is not None and (index + 1) >= hang_after_n:
                    # Wait for cancel up to 10s, polling stdin.
                    deadline = time.time() + 10.0
                    while time.time() < deadline and not cancel_pending:
                        nxt = _read_one_line(timeout_s=0.1)
                        if not nxt:
                            continue
                        with contextlib.suppress(json.JSONDecodeError):
                            inner = json.loads(nxt)
                            if isinstance(inner, dict) and inner.get("method") == "session/cancel":
                                cancel_pending = True
                    break

            final_stop = "cancelled" if cancel_pending else stop_reason
            cancel_pending = False
            _emit(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {"stopReason": final_stop},
                }
            )
            continue

        # Unknown method — reply with an error so the client doesn't hang.
        if rid is not None:
            _emit(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )


if __name__ == "__main__":
    raise SystemExit(main())
