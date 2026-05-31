"""Existing adapters must accept the new ``model`` keyword without error.

Stage M+ adds runtime model selection to the :class:`AgentBackend`
contract. Adapters whose underlying CLI cannot switch models at runtime
(claude_code, kiro_code) are required to *accept* the parameter and
ignore it; this test pins the contract at the boundary so a future
"strict signature" regression breaks loudly.
"""

from __future__ import annotations

from stratoclave_loom import BackendConfig, create_session


async def test_mock_send_message_accepts_model_param() -> None:
    cfg = BackendConfig(backend="mock", cwd=".", env={})
    chunks_seen = 0
    async with create_session(cfg) as session:
        stream = await session.send_message("hi", model="anything")
        async for chunk in stream:
            chunks_seen += 1
            if chunk.chunk_type == "end_turn":
                break
    assert chunks_seen >= 2  # at least one delta + end_turn


async def test_mock_default_model_id_is_none() -> None:
    cfg = BackendConfig(backend="mock", cwd=".", env={})
    async with create_session(cfg) as session:
        # Mock has no notion of a model.
        assert session.default_model_id is None


async def test_mock_list_models_is_empty_by_default() -> None:
    cfg = BackendConfig(backend="mock", cwd=".", env={})
    async with create_session(cfg) as session:
        assert await session.list_models() == ()
