"""Shared utilities for adapter implementations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def encode_jsonl(record: Mapping[str, Any]) -> bytes:
    """Encode a mapping as a single newline-terminated UTF-8 JSON line."""
    return (json.dumps(dict(record), ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def decode_jsonl_line(line: str) -> dict[str, Any] | None:
    """Decode a JSONL line, returning ``None`` on parse failure."""
    s = line.strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj
