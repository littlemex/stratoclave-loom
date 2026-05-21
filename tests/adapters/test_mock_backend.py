"""End-to-end-ish tests for the mock backend via the public API."""

from __future__ import annotations

from stratoclave_loom import BackendConfig, create_session


async def test_mock_streams_text_deltas_then_end_turn() -> None:
    cfg = BackendConfig(backend="mock", cwd=".", env={})
    types: list[str] = []
    text = ""
    async with create_session(cfg) as session:
        stream = await session.send_message("hi")
        async for chunk in stream:
            types.append(chunk.chunk_type)
            if chunk.chunk_type == "text_delta":
                text += chunk.content["text"]
            if chunk.chunk_type == "end_turn":
                break

    assert types[0] == "text_delta"
    assert types[-1] == "end_turn"
    assert text == "hi"


async def test_mock_cancellation_yields_error_chunk() -> None:
    cfg = BackendConfig(backend="mock", cwd=".", env={})
    seen: list[str] = []
    async with create_session(cfg) as session:
        await session.cancel()
        stream = await session.send_message("xyz")
        async for chunk in stream:
            seen.append(chunk.chunk_type)
            if chunk.chunk_type in ("end_turn", "error"):
                break

    assert seen[-1] == "error"
