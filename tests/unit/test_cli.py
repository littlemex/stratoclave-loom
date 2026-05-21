"""Smoke tests for the CLI entry point."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

from stratoclave_loom.cli import main


def test_list_backends_includes_built_ins() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["list-backends"])
    assert rc == 0
    listed = buf.getvalue().splitlines()
    assert "mock" in listed
    assert "claude_code" in listed


def test_run_against_mock_streams_jsonl() -> None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(
            [
                "run",
                "--backend",
                "mock",
                "--message",
                "ok",
            ]
        )
    assert rc == 0
    lines = [line for line in buf.getvalue().splitlines() if line.strip()]
    assert lines, "expected at least one chunk"
    parsed = [json.loads(line) for line in lines]
    types = [obj["chunk_type"] for obj in parsed]
    assert types[0] == "text_delta"
    assert types[-1] == "end_turn"
