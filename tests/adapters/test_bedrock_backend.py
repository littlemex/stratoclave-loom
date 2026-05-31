"""Unit tests for the Bedrock adapter with stubbed boto3 clients.

We do not exercise the real Bedrock service -- those tests live as
``e2e``-marked smokes. Here we substitute the adapter's
``_runtime`` / ``_control`` factories with hand-rolled stubs so we
can pin every observable behaviour deterministically.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from stratoclave_loom import BackendConfig, ModelFilter, create_session
from stratoclave_loom.adapters._registry import register_backend
from stratoclave_loom.adapters.bedrock import BedrockBackend

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
        {
            "modelId": "anthropic.claude-sonnet-4-20250514-v1:0",
            "modelName": "Claude Sonnet 4",
            "providerName": "Anthropic",
            "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["ON_DEMAND"],
            "responseStreamingSupported": True,
        },
        {
            # Excluded: not text-output capable.
            "modelId": "amazon.titan-image",
            "modelName": "Titan Image",
            "providerName": "Amazon",
            "outputModalities": ["IMAGE"],
            "inferenceTypesSupported": ["ON_DEMAND"],
            "responseStreamingSupported": True,
        },
        {
            # Excluded: provisioned-throughput only -- not callable from
            # converse_stream without a custom inference profile.
            "modelId": "meta.llama3-2-1b-instruct",
            "modelName": "Llama 3.2 1B",
            "providerName": "Meta",
            "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["PROVISIONED"],
            "responseStreamingSupported": True,
        },
        {
            # IP-only foundation model -- must be replaced by its
            # matching ``us.amazon.nova-lite-v1:0`` profile id below.
            "modelId": "amazon.nova-lite-v1:0",
            "modelName": "Nova Lite",
            "providerName": "Amazon",
            "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["INFERENCE_PROFILE"],
            "responseStreamingSupported": True,
        },
        {
            # IP-only foundation model with NO matching profile -- must
            # be dropped from the catalogue (uncallable from
            # ``converse_stream``).
            "modelId": "anthropic.claude-orphan-v1:0",
            "modelName": "Claude Orphan",
            "providerName": "Anthropic",
            "outputModalities": ["TEXT"],
            "inferenceTypesSupported": ["INFERENCE_PROFILE"],
            "responseStreamingSupported": True,
        },
    ]
}


_INFERENCE_PROFILES: dict[str, Any] = {
    "inferenceProfileSummaries": [
        {
            "inferenceProfileId": "us.amazon.nova-lite-v1:0",
            "inferenceProfileName": "US Nova Lite",
            "models": [
                {
                    "modelArn": (
                        "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-lite-v1:0"
                    ),
                },
            ],
        },
        {
            # Global-prefixed profile that should NOT win over a regional
            # one; here it covers a model that has no other profile so
            # the catalogue still picks it up.
            "inferenceProfileId": "global.amazon.nova-lite-v1:0",
            "inferenceProfileName": "Global Nova Lite",
            "models": [
                {
                    "modelArn": ("arn:aws:bedrock:::foundation-model/amazon.nova-lite-v1:0"),
                },
            ],
        },
    ]
}


class _StubControl:
    """Stands in for ``boto3.client('bedrock')`` calls."""

    def list_foundation_models(self) -> dict[str, Any]:
        return _LIST_PAYLOAD

    def list_inference_profiles(self) -> dict[str, Any]:
        return _INFERENCE_PROFILES


class _StubRuntime:
    """Stands in for ``boto3.client('bedrock-runtime')`` calls.

    ``converse_stream`` returns a synthetic event-stream that exercises
    the adapter's text-delta + messageStop branches.
    """

    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] | None = None
        self.scripted_text = ["Hello", " from", " Bedrock"]

    def converse_stream(self, **kwargs: Any) -> dict[str, Any]:
        self.last_kwargs = kwargs

        def _events() -> Iterator[dict[str, Any]]:
            for piece in self.scripted_text:
                yield {"contentBlockDelta": {"delta": {"text": piece}}}
            yield {"messageStop": {"stopReason": "end_turn"}}

        return {"stream": _events()}


@pytest.fixture
def bedrock_with_stubs(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[BedrockBackend, _StubRuntime]:
    """Register a fresh BedrockBackend whose clients are hand-rolled stubs."""

    runtime = _StubRuntime()
    backend = BedrockBackend()

    def fake_runtime(profile: str | None, region: str) -> _StubRuntime:
        return runtime

    def fake_control(profile: str | None, region: str) -> _StubControl:
        return _StubControl()

    monkeypatch.setattr(backend, "_runtime", fake_runtime)
    monkeypatch.setattr(backend, "_control", fake_control)
    # Re-register so create_session() picks up our stubbed instance.
    register_backend("bedrock", lambda: backend)
    return backend, runtime


async def test_list_models_filters_by_modality_and_inference_type(
    bedrock_with_stubs: tuple[BedrockBackend, _StubRuntime],
) -> None:
    _backend, _ = bedrock_with_stubs
    cfg = BackendConfig(backend="bedrock", cwd=".", extra={"aws_region": "us-east-1"})
    async with create_session(cfg) as session:
        models = await session.list_models()
    ids = [m.id for m in models]
    # Image-only and provisioned-only entries are excluded.
    assert "amazon.titan-image" not in ids
    assert "meta.llama3-2-1b-instruct" not in ids
    # ON_DEMAND text-capable models keep their bare id.
    assert "anthropic.claude-opus-4-7-20251015-v1:0" in ids
    assert "anthropic.claude-sonnet-4-20250514-v1:0" in ids
    # IP-only foundation models surface as their inference-profile id;
    # the bare ``amazon.nova-lite-v1:0`` would 4xx from converse_stream.
    assert "amazon.nova-lite-v1:0" not in ids
    assert "us.amazon.nova-lite-v1:0" in ids
    # IP-only foundation model with no matching profile is dropped.
    assert "anthropic.claude-orphan-v1:0" not in ids


async def test_list_models_applies_model_filter(
    bedrock_with_stubs: tuple[BedrockBackend, _StubRuntime],
) -> None:
    _backend, _ = bedrock_with_stubs
    cfg = BackendConfig(backend="bedrock", cwd=".")
    async with create_session(cfg) as session:
        opus_only = await session.list_models(ModelFilter(substring="opus"))
    assert {m.id for m in opus_only} == {"anthropic.claude-opus-4-7-20251015-v1:0"}


async def test_default_picks_opus_4_7_first(
    bedrock_with_stubs: tuple[BedrockBackend, _StubRuntime],
) -> None:
    _backend, runtime = bedrock_with_stubs
    cfg = BackendConfig(backend="bedrock", cwd=".")
    async with create_session(cfg) as session:
        # First send_message with model=None should pick opus-4-7.
        stream = await session.send_message("hi")
        async for chunk in stream:
            if chunk.chunk_type == "end_turn":
                break
    assert runtime.last_kwargs is not None
    assert runtime.last_kwargs["modelId"] == "anthropic.claude-opus-4-7-20251015-v1:0"


async def test_explicit_model_overrides_default(
    bedrock_with_stubs: tuple[BedrockBackend, _StubRuntime],
) -> None:
    _backend, runtime = bedrock_with_stubs
    cfg = BackendConfig(backend="bedrock", cwd=".")
    async with create_session(cfg) as session:
        stream = await session.send_message(
            "hi",
            model="amazon.nova-lite-v1:0",
        )
        async for chunk in stream:
            if chunk.chunk_type == "end_turn":
                break
    assert runtime.last_kwargs is not None
    assert runtime.last_kwargs["modelId"] == "amazon.nova-lite-v1:0"


async def test_model_choice_sticks_until_overridden(
    bedrock_with_stubs: tuple[BedrockBackend, _StubRuntime],
) -> None:
    _backend, runtime = bedrock_with_stubs
    cfg = BackendConfig(backend="bedrock", cwd=".")
    async with create_session(cfg) as session:
        # Pick once with explicit model.
        async for chunk in await session.send_message("a", model="amazon.nova-lite-v1:0"):
            if chunk.chunk_type == "end_turn":
                break
        # Next call with model=None must reuse the last explicit pick,
        # not silently fall back to the catalogue default.
        async for chunk in await session.send_message("b"):
            if chunk.chunk_type == "end_turn":
                break
    assert runtime.last_kwargs is not None
    assert runtime.last_kwargs["modelId"] == "amazon.nova-lite-v1:0"


async def test_history_arg_is_replayed_into_messages(
    bedrock_with_stubs: tuple[BedrockBackend, _StubRuntime],
) -> None:
    """Plan-A: the host rebuilds the conversation each turn and passes
    it via ``history``. Bedrock keeps no internal buffer; the messages
    sent to ``converse_stream`` must reflect exactly what the host
    supplied, plus the new ``content`` as the trailing user turn.
    """

    _backend, runtime = bedrock_with_stubs
    cfg = BackendConfig(backend="bedrock", cwd=".")
    history = (
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
        {"role": "user", "content": "remember the number 42"},
        {"role": "assistant", "content": "noted"},
    )
    async with create_session(cfg) as session:
        async for chunk in await session.send_message("what number?", history=history):
            if chunk.chunk_type == "end_turn":
                break
    assert runtime.last_kwargs is not None
    sent = runtime.last_kwargs["messages"]
    # Four history entries + one new user turn.
    assert len(sent) == 5
    assert [m["role"] for m in sent] == ["user", "assistant", "user", "assistant", "user"]
    assert sent[-1]["content"] == [{"text": "what number?"}]
    assert sent[2]["content"] == [{"text": "remember the number 42"}]


async def test_no_internal_buffer_between_calls(
    bedrock_with_stubs: tuple[BedrockBackend, _StubRuntime],
) -> None:
    """Pin the stateless contract: a second send_message with no
    ``history`` must NOT include the first call's messages. Plan-A
    requires the adapter to forget what it sent last turn -- the host
    is the single source of truth.
    """

    _backend, runtime = bedrock_with_stubs
    cfg = BackendConfig(backend="bedrock", cwd=".")
    async with create_session(cfg) as session:
        async for chunk in await session.send_message("first"):
            if chunk.chunk_type == "end_turn":
                break
        async for chunk in await session.send_message("second"):
            if chunk.chunk_type == "end_turn":
                break
    sent = runtime.last_kwargs["messages"]
    # Only the second prompt should be in the messages list -- if the
    # adapter still buffered "first" we'd see it here.
    assert len(sent) == 1
    assert sent[0]["role"] == "user"
    assert sent[0]["content"] == [{"text": "second"}]


async def test_streaming_yields_text_deltas_and_end_turn(
    bedrock_with_stubs: tuple[BedrockBackend, _StubRuntime],
) -> None:
    _backend, _ = bedrock_with_stubs
    cfg = BackendConfig(backend="bedrock", cwd=".")
    pieces: list[str] = []
    chunks_types: list[str] = []
    async with create_session(cfg) as session:
        stream = await session.send_message("hi")
        async for chunk in stream:
            chunks_types.append(chunk.chunk_type)
            if chunk.chunk_type == "text_delta":
                pieces.append(str(chunk.content.get("text", "")))
            if chunk.chunk_type == "end_turn":
                break
    assert "".join(pieces) == "Hello from Bedrock"
    assert chunks_types[-1] == "end_turn"


async def test_inference_profile_metadata_is_recorded(
    bedrock_with_stubs: tuple[BedrockBackend, _StubRuntime],
) -> None:
    """``ModelInfo.extra`` records whether the id maps via a profile.

    Pinning the metadata so picker UIs can grey out / annotate IP-only
    entries if they want, and so the catalogue stays inspectable for
    debugging when an operator wonders why a model id was rewritten.
    """

    _backend, _ = bedrock_with_stubs
    cfg = BackendConfig(backend="bedrock", cwd=".", extra={"aws_region": "us-east-1"})
    async with create_session(cfg) as session:
        models = await session.list_models()
    by_id = {m.id: m for m in models}
    nova = by_id["us.amazon.nova-lite-v1:0"]
    assert nova.extra["via_inference_profile"] is True
    assert nova.extra["foundation_model_id"] == "amazon.nova-lite-v1:0"
    on_demand = by_id["anthropic.claude-opus-4-7-20251015-v1:0"]
    assert on_demand.extra["via_inference_profile"] is False
    assert on_demand.extra["foundation_model_id"] == on_demand.id


async def test_regional_profile_wins_over_global(
    bedrock_with_stubs: tuple[BedrockBackend, _StubRuntime],
) -> None:
    """When both a regional ``us.*`` and a ``global.*`` profile cover
    the same foundation model, the catalogue prefers the regional one.
    """

    _backend, _ = bedrock_with_stubs
    cfg = BackendConfig(backend="bedrock", cwd=".")
    async with create_session(cfg) as session:
        models = await session.list_models()
    ids = [m.id for m in models]
    assert "us.amazon.nova-lite-v1:0" in ids
    assert "global.amazon.nova-lite-v1:0" not in ids


async def test_substring_default_override_via_extra(
    bedrock_with_stubs: tuple[BedrockBackend, _StubRuntime],
) -> None:
    _backend, runtime = bedrock_with_stubs
    cfg = BackendConfig(
        backend="bedrock",
        cwd=".",
        extra={"default_model_substring": "nova"},
    )
    async with create_session(cfg) as session:
        async for chunk in await session.send_message("hi"):
            if chunk.chunk_type == "end_turn":
                break
    assert runtime.last_kwargs is not None
    # The catalogue surfaces ``us.amazon.nova-lite-v1:0`` (the
    # invocable inference-profile id) for the IP-only nova foundation
    # model, so the auto-pick lands there too.
    assert runtime.last_kwargs["modelId"] == "us.amazon.nova-lite-v1:0"
