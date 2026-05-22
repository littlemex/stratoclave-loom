"""Standalone stub used in place of the real ``claude`` CLI.

The stub's behaviour is configured via environment variables so the test
file can describe scenarios declaratively without templating Python source.

Environment variables
---------------------

CLAUDE_STUB_SCRIPT
    Path to a JSONL file. Each line is emitted on stdout in order.

CLAUDE_STUB_DELAY_MS
    Optional. Milliseconds to sleep between lines. Defaults to 0.

CLAUDE_STUB_HANG_AFTER_LINES
    Optional. Number of lines to emit before sleeping forever (used to
    exercise the cancel path). Defaults to no hang.

CLAUDE_STUB_RECORD_ARGV
    Optional. Path to write the received argv as JSON.

CLAUDE_STUB_RECORD_STDIN
    Optional. Path to write the received stdin payload.

The stub never imports stratoclave_loom on purpose so that subprocess
spawn cost stays minimal.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import time


def main() -> int:
    argv_path = os.environ.get("CLAUDE_STUB_RECORD_ARGV")
    if argv_path:
        with open(argv_path, "w", encoding="utf-8") as fh:
            json.dump(sys.argv, fh)

    stdin_path = os.environ.get("CLAUDE_STUB_RECORD_STDIN")
    if stdin_path:
        # Drain stdin so the parent's drain() returns. We do not need the
        # content for assertions, just that the parent could write+EOF it.
        data = sys.stdin.read()
        with open(stdin_path, "w", encoding="utf-8") as fh:
            fh.write(data)
    else:
        # Even when not recording, drain stdin to avoid the parent hanging
        # on drain() when stdin's pipe buffer fills.
        with contextlib.suppress(OSError):
            sys.stdin.read()

    script_path = os.environ.get("CLAUDE_STUB_SCRIPT")
    if not script_path:
        return 0

    delay_ms = int(os.environ.get("CLAUDE_STUB_DELAY_MS", "0"))
    hang_after = os.environ.get("CLAUDE_STUB_HANG_AFTER_LINES")
    hang_after_n = int(hang_after) if hang_after else None

    with open(script_path, encoding="utf-8") as fh:
        for index, raw in enumerate(fh):
            line = raw.rstrip("\n")
            if not line:
                continue
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
            if hang_after_n is not None and (index + 1) >= hang_after_n:
                while True:
                    time.sleep(60)
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
