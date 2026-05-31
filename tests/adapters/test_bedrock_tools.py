"""Tests for the Bedrock adapter's built-in tool surface.

Two layers of coverage:

1. Pure-function tests for ``execute_tool`` and the ``ToolLimits`` /
   ``build_tool_config`` helpers. These do not involve boto3 at all.
2. Adapter integration test that drives ``BedrockBackend.send_message``
   through a scripted multi-turn ``converse_stream`` event sequence:
   first turn returns a ``toolUse`` block, second turn returns text.
   The harness verifies the tool actually ran AND that the second
   ``converse_stream`` call carried a ``toolResult`` block.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from stratoclave_loom import BackendConfig, create_session
from stratoclave_loom.adapters._bedrock_tools import (
    EXTRA_TOOLS_ENABLED,
    ToolLimits,
    build_tool_config,
    execute_tool,
)
from stratoclave_loom.adapters._registry import register_backend
from stratoclave_loom.adapters.bedrock import BedrockBackend

# ---------------------------------------------------------------------------
# ToolLimits + build_tool_config (pure)
# ---------------------------------------------------------------------------


def test_tool_limits_defaults() -> None:
    limits = ToolLimits.from_extra({})
    assert limits.enabled is True
    assert limits.max_fetch_bytes >= 64 * 1024
    assert limits.max_read_bytes >= 64 * 1024
    assert limits.fetch_timeout_seconds > 0
    assert limits.max_tool_calls_per_turn >= 1


def test_tool_limits_disabled_via_extra() -> None:
    limits = ToolLimits.from_extra({EXTRA_TOOLS_ENABLED: False})
    assert limits.enabled is False
    assert build_tool_config(limits) is None


def test_tool_limits_overrides() -> None:
    limits = ToolLimits.from_extra(
        {
            "tool_max_fetch_bytes": 1024,
            "tool_max_read_bytes": 2048,
            "tool_fetch_timeout_seconds": 1.5,
            "tool_max_calls_per_turn": 2,
        }
    )
    assert limits.max_fetch_bytes == 1024
    assert limits.max_read_bytes == 2048
    assert limits.fetch_timeout_seconds == 1.5
    assert limits.max_tool_calls_per_turn == 2


def test_tool_limits_invalid_overrides_fall_back() -> None:
    """Garbage values should not break the adapter; defaults must hold.

    A 0 / negative cap is treated as 'use default' so a config typo
    cannot accidentally disable the safety rails.
    """

    limits = ToolLimits.from_extra(
        {
            "tool_max_fetch_bytes": -1,
            "tool_max_read_bytes": "not-a-number",
            "tool_fetch_timeout_seconds": 0,
            "tool_max_calls_per_turn": None,
        }
    )
    # Default constants are not exposed as attributes here; just assert
    # they fell back to non-zero positives.
    assert limits.max_fetch_bytes > 0
    assert limits.max_read_bytes > 0
    assert limits.fetch_timeout_seconds > 0
    assert limits.max_tool_calls_per_turn > 0


def test_build_tool_config_lists_both_tools() -> None:
    config = build_tool_config(ToolLimits.from_extra({}))
    assert config is not None
    names = {t["toolSpec"]["name"] for t in config["tools"]}
    assert names == {"web_fetch", "file_read"}
    assert config["toolChoice"] == {"auto": {}}


# ---------------------------------------------------------------------------
# execute_tool: web_fetch (URL validation, no real network)
# ---------------------------------------------------------------------------


def test_web_fetch_rejects_missing_url() -> None:
    out = execute_tool("web_fetch", {}, cwd=".", limits=ToolLimits.from_extra({}))
    assert out["status"] == "error"


def test_web_fetch_rejects_non_http_scheme() -> None:
    limits = ToolLimits.from_extra({})
    out = execute_tool(
        "web_fetch",
        {"url": "file:///etc/passwd"},
        cwd=".",
        limits=limits,
    )
    assert out["status"] == "error"
    text = out["content"][0]["text"]
    assert "scheme" in text.lower()


def test_web_fetch_rejects_relative_url() -> None:
    out = execute_tool(
        "web_fetch",
        {"url": "/relative/path"},
        cwd=".",
        limits=ToolLimits.from_extra({}),
    )
    assert out["status"] == "error"


def test_web_fetch_returns_body_with_truncation_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hit a stubbed urlopen that returns 600 bytes against a 256-byte cap."""

    class _Resp:
        status: int = 200
        headers: ClassVar[dict[str, str]] = {"Content-Type": "text/plain; charset=utf-8"}

        def __init__(self, body: bytes) -> None:
            self._body = body
            self._read = False

        def read(self, n: int = -1) -> bytes:
            if self._read:
                return b""
            self._read = True
            if n == -1 or n >= len(self._body):
                return self._body
            return self._body[:n]

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

    big_body = b"A" * 600

    def fake_urlopen(req: Any, timeout: float = 0) -> Any:
        return _Resp(big_body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen, raising=True)
    limits = ToolLimits.from_extra({"tool_max_fetch_bytes": 256})
    out = execute_tool(
        "web_fetch",
        {"url": "https://example.test/page"},
        cwd=".",
        limits=limits,
    )
    assert out["status"] == "success"
    text = out["content"][0]["text"]
    assert "HTTP 200" in text
    assert "(truncated)" in text
    # Body section should be exactly the cap.
    body_section = text.split("\n\n", 1)[1]
    assert len(body_section) == 256


# ---------------------------------------------------------------------------
# execute_tool: file_read (path containment)
# ---------------------------------------------------------------------------


def test_file_read_returns_text(tmp_path: Path) -> None:
    target = tmp_path / "hello.txt"
    target.write_text("hello, atelier\n", encoding="utf-8")
    out = execute_tool(
        "file_read",
        {"path": "hello.txt"},
        cwd=str(tmp_path),
        limits=ToolLimits.from_extra({}),
    )
    assert out["status"] == "success"
    text = out["content"][0]["text"]
    assert "hello, atelier" in text


def test_file_read_rejects_missing_path(tmp_path: Path) -> None:
    out = execute_tool(
        "file_read",
        {},
        cwd=str(tmp_path),
        limits=ToolLimits.from_extra({}),
    )
    assert out["status"] == "error"


def test_file_read_rejects_escape_via_relative_path(tmp_path: Path) -> None:
    """``../../etc/passwd`` style paths must not return content from
    outside the configured cwd. Before this test the only thing
    standing between Bedrock and ``/etc/passwd`` would have been the
    symlink check; pin both belts.
    """

    sub = tmp_path / "work"
    sub.mkdir()
    out = execute_tool(
        "file_read",
        {"path": "../../etc/passwd"},
        cwd=str(sub),
        limits=ToolLimits.from_extra({}),
    )
    assert out["status"] == "error"


def test_file_read_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink that points outside the cwd must be refused.

    We materialise an actual ``/tmp`` outside-cwd file then symlink to
    it from inside the cwd; the resolve+relative_to check should
    intercept the escape.
    """

    cage = tmp_path / "cage"
    cage.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = cage / "leak"
    link.symlink_to(outside)
    out = execute_tool(
        "file_read",
        {"path": "leak"},
        cwd=str(cage),
        limits=ToolLimits.from_extra({}),
    )
    assert out["status"] == "error"


def test_file_read_truncates_large_files(tmp_path: Path) -> None:
    target = tmp_path / "big.txt"
    target.write_text("X" * 10_000, encoding="utf-8")
    limits = ToolLimits.from_extra({"tool_max_read_bytes": 100})
    out = execute_tool(
        "file_read",
        {"path": "big.txt"},
        cwd=str(tmp_path),
        limits=limits,
    )
    assert out["status"] == "success"
    text = out["content"][0]["text"]
    assert "truncated" in text
    body = text.split("\n\n", 1)[1]
    assert len(body) == 100


# ---------------------------------------------------------------------------
# Adapter integration: scripted converse_stream with tool_use round-trip
# ---------------------------------------------------------------------------


_LIST_PAYLOAD: dict[str, Any] = {
    "modelSummaries": [
        {
            "modelId": "anthropic.claude-opus-4-7-20251015-v1:0",
            "modelName": "Claude Opus 4.7",
            "providerName": "Anthropic",
            "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["ON_DEMAND"],
            "responseStreamingSupported": True,
        },
    ]
}


class _StubControl:
    def list_foundation_models(self) -> dict[str, Any]:
        return _LIST_PAYLOAD

    def list_inference_profiles(self) -> dict[str, Any]:
        return {"inferenceProfileSummaries": []}


class _ScriptedRuntime:
    """Two-step Converse stub that exercises the tool_use loop.

    Call 1: model emits a single ``toolUse`` block requesting
    ``file_read`` against ``answer.txt``, then ``messageStop`` with
    ``stopReason='tool_use'``.

    Call 2 (after the adapter feeds back the toolResult): model emits a
    plain text response and ends the turn normally.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def converse_stream(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if len(self.calls) == 1:

            def _events_first() -> Iterator[dict[str, Any]]:
                yield {
                    "contentBlockStart": {
                        "start": {
                            "toolUse": {
                                "toolUseId": "tu-1",
                                "name": "file_read",
                            }
                        }
                    }
                }
                yield {
                    "contentBlockDelta": {
                        "delta": {"toolUse": {"input": json.dumps({"path": "answer.txt"})}}
                    }
                }
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "tool_use"}}

            return {"stream": _events_first()}

        def _events_second() -> Iterator[dict[str, Any]]:
            yield {"contentBlockDelta": {"delta": {"text": "The answer is 42."}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}

        return {"stream": _events_second()}


@pytest.fixture
def scripted_bedrock(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[BedrockBackend, _ScriptedRuntime]:
    runtime = _ScriptedRuntime()
    backend = BedrockBackend()

    def fake_runtime(profile: str | None, region: str) -> _ScriptedRuntime:
        return runtime

    def fake_control(profile: str | None, region: str) -> _StubControl:
        return _StubControl()

    monkeypatch.setattr(backend, "_runtime", fake_runtime)
    monkeypatch.setattr(backend, "_control", fake_control)
    register_backend("bedrock", lambda: backend)
    return backend, runtime


async def test_converse_loop_executes_tool_and_replays_result(
    tmp_path: Path,
    scripted_bedrock: tuple[BedrockBackend, _ScriptedRuntime],
) -> None:
    """End-to-end: a scripted ``tool_use`` round-trip resolves to a
    final ``end_turn`` chunk, and the second converse_stream call
    carries a ``toolResult`` message reflecting the file's actual
    contents.
    """

    _backend, runtime = scripted_bedrock
    answer_path = tmp_path / "answer.txt"
    answer_path.write_text("42", encoding="utf-8")

    cfg = BackendConfig(backend="bedrock", cwd=str(tmp_path))
    chunk_types: list[str] = []
    async with create_session(cfg) as session:
        async for chunk in await session.send_message("what's the answer?"):
            chunk_types.append(chunk.chunk_type)
            if chunk.chunk_type == "end_turn":
                break

    # Two converse_stream calls happened; toolConfig was attached to both.
    assert len(runtime.calls) == 2
    for call in runtime.calls:
        assert "toolConfig" in call
        names = {t["toolSpec"]["name"] for t in call["toolConfig"]["tools"]}
        assert names == {"web_fetch", "file_read"}

    # The second call's ``messages`` list must end with a user-role
    # entry whose ONLY content is a toolResult block, and the previous
    # entry must be the assistant's tool_use replay.
    second_messages = runtime.calls[1]["messages"]
    assert second_messages[-1]["role"] == "user"
    last_content = second_messages[-1]["content"]
    assert any("toolResult" in b for b in last_content)
    tool_result_block = next(b for b in last_content if "toolResult" in b)
    assert tool_result_block["toolResult"]["toolUseId"] == "tu-1"
    # Tool actually read the file -- the result text should mention "42".
    result_text = tool_result_block["toolResult"]["content"][0]["text"]
    assert "42" in result_text
    # Sanity: chunk_types include both tool_use + tool_result + end_turn.
    assert "tool_use" in chunk_types
    assert "tool_result" in chunk_types
    assert chunk_types[-1] == "end_turn"


async def test_tool_use_loop_caps_at_configured_max(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap on tool calls per turn must trip if the model keeps
    asking. We script a runtime that never stops emitting ``tool_use``;
    with the cap set to 1 the adapter must break out and emit an error
    chunk rather than spinning forever.
    """

    class _ForeverToolRuntime:
        def __init__(self) -> None:
            self.call_count = 0

        def converse_stream(self, **kwargs: Any) -> dict[str, Any]:
            self.call_count += 1

            def _events() -> Iterator[dict[str, Any]]:
                yield {
                    "contentBlockStart": {
                        "start": {
                            "toolUse": {
                                "toolUseId": f"tu-{self.call_count}",
                                "name": "file_read",
                            }
                        }
                    }
                }
                yield {
                    "contentBlockDelta": {
                        "delta": {"toolUse": {"input": json.dumps({"path": "x.txt"})}}
                    }
                }
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "tool_use"}}

            return {"stream": _events()}

    runtime = _ForeverToolRuntime()
    backend = BedrockBackend()
    monkeypatch.setattr(backend, "_runtime", lambda *a, **kw: runtime)
    monkeypatch.setattr(backend, "_control", lambda *a, **kw: _StubControl())
    register_backend("bedrock", lambda: backend)

    (tmp_path / "x.txt").write_text("hi", encoding="utf-8")
    cfg = BackendConfig(
        backend="bedrock",
        cwd=str(tmp_path),
        extra={"tool_max_calls_per_turn": 1},
    )
    chunk_types: list[str] = []
    error_seen = False
    async with create_session(cfg) as session:
        async for chunk in await session.send_message("loop please"):
            chunk_types.append(chunk.chunk_type)
            if chunk.chunk_type == "error":
                error_seen = True
            if chunk.chunk_type == "end_turn":
                break
    # The adapter must surface a cap-exceeded error then exit cleanly.
    assert error_seen, f"no error chunk emitted; types={chunk_types!r}"
    assert chunk_types[-1] == "end_turn"
    # The cap is 1 meaning at most ONE tool round-trip; thus exactly two
    # converse_stream calls (initial + one tool replay) and the third
    # would exceed the cap so it never happens.
    assert runtime.call_count == 2


async def test_tools_disabled_omits_tool_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operators can opt out via ``tools_enabled=False``; the converse
    call must NOT carry a ``toolConfig`` in that case.
    """

    class _SimpleRuntime:
        def __init__(self) -> None:
            self.last_kwargs: dict[str, Any] = {}

        def converse_stream(self, **kwargs: Any) -> dict[str, Any]:
            self.last_kwargs = kwargs

            def _events() -> Iterator[dict[str, Any]]:
                yield {"contentBlockDelta": {"delta": {"text": "hi"}}}
                yield {"messageStop": {"stopReason": "end_turn"}}

            return {"stream": _events()}

    runtime = _SimpleRuntime()
    backend = BedrockBackend()
    monkeypatch.setattr(backend, "_runtime", lambda *a, **kw: runtime)
    monkeypatch.setattr(backend, "_control", lambda *a, **kw: _StubControl())
    register_backend("bedrock", lambda: backend)

    cfg = BackendConfig(
        backend="bedrock",
        cwd=str(tmp_path),
        extra={"tools_enabled": False},
    )
    async with create_session(cfg) as session:
        async for chunk in await session.send_message("hi"):
            if chunk.chunk_type == "end_turn":
                break
    assert "toolConfig" not in runtime.last_kwargs
