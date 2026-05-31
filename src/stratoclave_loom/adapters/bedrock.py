"""AWS Bedrock backend — calls the Converse API natively, no subprocess.

This adapter speaks directly to the Bedrock Runtime ``converse_stream``
API (and the control-plane ``list_foundation_models``) via boto3, so it
sits next to the CLI-backed adapters as just another :class:`AgentBackend`.
The host application never has to know the difference.

Why a native adapter
--------------------
* Switching models per call is a string parameter on ``converse_stream``;
  no subprocess restart, no env-var dance.
* No CLI binary needs to be installed on the host -- handy for serverless
  or container deployments where shelling out is expensive or impossible.
* Tool-use, system prompts, inference parameters, and streaming all flow
  through one SDK surface.

Configuration
-------------
``BackendConfig.extra`` knobs (all optional; sensible defaults apply):

* ``aws_profile`` -- ``boto3.Session(profile_name=...)`` override.
  Falls back to ``$AWS_PROFILE`` then default credential resolution.
* ``aws_region``  -- region for both the runtime and control-plane
  clients. Falls back to ``$AWS_REGION`` / ``$AWS_DEFAULT_REGION``,
  then ``us-east-1``.
* ``default_model_substring`` -- substring used to rank the auto-picked
  default. Defaults to ``"opus-4-7"`` then ``"opus-4"`` then
  ``"sonnet-4"`` (in order). The first model whose id or name contains
  the substring wins; if nothing matches we fall back to the first
  streaming-capable text model returned by ``list_foundation_models``.
* ``system_prompt`` -- system message prepended to every turn.
* ``temperature`` / ``max_tokens`` / ``top_p`` -- standard inference
  knobs forwarded into ``inferenceConfig``.

Authentication is delegated entirely to the boto3 credential chain:
the loom adapter does not handle, persist, or transform credentials.
The host (atelier, CLI, …) is responsible for ensuring that whichever
profile / role / instance metadata is in scope can call Bedrock.

Tool use
--------
The adapter ships two read-only built-in tools so a chat session can
look things up: ``web_fetch`` (HTTP GET, capped) and ``file_read``
(UTF-8 file read confined to ``BackendConfig.cwd``). Tool execution is
driven by a multi-step Converse loop -- the model emits ``toolUse``
blocks, the adapter executes them inside hard limits
(``MAX_TOOL_CALLS_PER_TURN``, byte caps, fetch timeout) and feeds
``toolResult`` blocks back into the next ``converse_stream`` call. Tool
use is opt-out via ``BackendConfig.extra['tools_enabled'] = False``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any

from stratoclave_loom.adapters._bedrock_tools import (
    ToolLimits,
    build_tool_config,
    execute_tool,
)
from stratoclave_loom.adapters._registry import register_backend
from stratoclave_loom.core.backend import AgentBackend
from stratoclave_loom.core.errors import AdapterError, SessionClosedError
from stratoclave_loom.core.types import (
    AcpChunk,
    BackendConfig,
    ModelFilter,
    ModelInfo,
    NormalizedTurn,
    PermissionRequest,
)

logger = logging.getLogger(__name__)


# Substrings tried in order when the caller does not pin a default. The
# first model whose id or name contains the substring (case-insensitive)
# becomes the suggested default.
_DEFAULT_PREFERENCE = ("opus-4-7", "opus-4", "sonnet-4-7", "sonnet-4")


def _coerce_str(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


class _SessionState:
    """Per-session state held by the Bedrock backend.

    Plan-A keeps this struct intentionally narrow: the adapter no
    longer remembers conversation messages -- the host rebuilds the
    list from its event log every turn and passes it via ``history``.
    What survives here is *adapter-shaped* config that does not change
    turn-to-turn (system prompt, inference knobs) and the most-recent
    model pick, which is sticky inside one session.
    """

    __slots__ = (
        "active",
        "config",
        "current_model_id",
        "inference_config",
        "system_prompt",
        "tool_limits",
    )

    def __init__(
        self,
        config: BackendConfig,
        *,
        system_prompt: str | None,
        inference_config: dict[str, Any],
        tool_limits: ToolLimits,
    ) -> None:
        self.config = config
        self.current_model_id: str | None = None
        self.system_prompt = system_prompt
        self.inference_config = inference_config
        self.tool_limits = tool_limits
        self.active = True


def _normalise_history(
    history: tuple[Mapping[str, Any], ...] | None,
) -> list[dict[str, Any]]:
    """Convert the host-supplied turn list into Bedrock's ``messages`` shape.

    Each entry in ``history`` carries ``role`` (``user`` / ``assistant``)
    and ``content`` (string). Bedrock Converse expects
    ``[{"role": "...", "content": [{"text": "..."}]}]``; we wrap the
    string verbatim. Roles other than user / assistant are dropped --
    Bedrock would reject them and we want a clean trace rather than a
    400 from the API.
    """

    if not history:
        return []
    out: list[dict[str, Any]] = []
    for entry in history:
        role = str(entry.get("role", "")).lower()
        if role not in ("user", "assistant"):
            continue
        content = entry.get("content", "")
        blocks = list(content) if isinstance(content, list) else [{"text": str(content)}]
        out.append({"role": role, "content": blocks})
    return out


class BedrockBackend(AgentBackend):
    """Native Bedrock backend driven by boto3 ``converse_stream``."""

    backend_name = "bedrock"

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionState] = {}
        # Lazily-initialised boto3 clients keyed by ``(profile, region)``
        # so multiple sessions across regions reuse the same client.
        self._runtime_clients: dict[tuple[str | None, str], Any] = {}
        self._control_clients: dict[tuple[str | None, str], Any] = {}
        # Cached catalogue per (profile, region). Bedrock's
        # ``list_foundation_models`` rarely changes, so we cache once
        # per process and re-list on demand by recreating the adapter.
        self._catalogue_cache: dict[tuple[str | None, str], tuple[ModelInfo, ...]] = {}
        self._fallback_default: str | None = None

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _resolve_profile(extra: Mapping[str, Any]) -> str | None:
        return _coerce_str(extra.get("aws_profile")) or _coerce_str(os.environ.get("AWS_PROFILE"))

    @staticmethod
    def _resolve_region(extra: Mapping[str, Any]) -> str:
        return (
            _coerce_str(extra.get("aws_region"))
            or _coerce_str(os.environ.get("AWS_REGION"))
            or _coerce_str(os.environ.get("AWS_DEFAULT_REGION"))
            or "us-east-1"
        )

    def _boto3_session(self, profile: str | None, region: str) -> Any:
        """Build a fresh ``boto3.Session``; called once per client cache miss."""

        try:
            import boto3  # type: ignore[import-untyped]  # boto3 is optional
        except ImportError as exc:  # pragma: no cover - unit-tested via skip
            raise AdapterError(
                "stratoclave-loom[bedrock] extra is required for the bedrock "
                "backend; install boto3 to enable it"
            ) from exc
        return boto3.session.Session(profile_name=profile, region_name=region)

    def _runtime(self, profile: str | None, region: str) -> Any:
        key = (profile, region)
        client = self._runtime_clients.get(key)
        if client is None:
            client = self._boto3_session(profile, region).client("bedrock-runtime")
            self._runtime_clients[key] = client
        return client

    def _control(self, profile: str | None, region: str) -> Any:
        key = (profile, region)
        client = self._control_clients.get(key)
        if client is None:
            # ``bedrock`` (no -runtime suffix) is the control-plane
            # client that owns ``list_foundation_models`` and friends.
            client = self._boto3_session(profile, region).client("bedrock")
            self._control_clients[key] = client
        return client

    def _ensure_session(self, session_id: str) -> _SessionState:
        state = self._sessions.get(session_id)
        if state is None or not state.active:
            raise SessionClosedError(f"session {session_id!r} is not active")
        return state

    # -- AgentBackend contract --------------------------------------------

    async def initialize(
        self,
        session_id: str,
        config: BackendConfig,
        capabilities: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        # Pull adapter-specific knobs out of ``extra`` once at session
        # warmup so ``send_message`` stays free of config plumbing.
        extra = config.extra
        system_prompt = _coerce_str(extra.get("system_prompt"))
        inference_config: dict[str, Any] = {}
        for key in ("temperature", "top_p", "max_tokens"):
            value = extra.get(key)
            if value is None:
                continue
            # Bedrock expects camelCase here.
            mapped = {"max_tokens": "maxTokens", "top_p": "topP"}.get(key, key)
            inference_config[mapped] = value

        self._sessions[session_id] = _SessionState(
            config=config,
            system_prompt=system_prompt,
            inference_config=inference_config,
            tool_limits=ToolLimits.from_extra(extra),
        )
        # Pre-pick a default model so callers that pass ``model=None``
        # on the first turn have something to talk to.
        default = await self._pick_default_model(config)
        if default is not None:
            self._sessions[session_id].current_model_id = default
        agreed = dict(capabilities)
        agreed.setdefault("model_routing", "runtime")
        return agreed

    async def send_message(
        self,
        session_id: str,
        content: str,
        *,
        context_files: tuple[str, ...] = (),
        model: str | None = None,
        history: tuple[Mapping[str, Any], ...] | None = None,
    ) -> AsyncIterator[AcpChunk]:
        state = self._ensure_session(session_id)
        # Resolve the effective model: explicit > sticky last pick > default.
        effective_model = (
            _coerce_str(model)
            or state.current_model_id
            or await self._pick_default_model(state.config)
        )
        if effective_model is None:
            raise AdapterError(
                "no Bedrock model available; configure default_model_substring "
                "in BackendConfig.extra or pass model= explicitly"
            )
        state.current_model_id = effective_model
        # Plan-A: the host (atelier) reconstructs the conversation from
        # its event log every turn and passes it via ``history``. We
        # therefore do NOT keep a local message buffer -- Bedrock
        # ``converse_stream`` is stateless, so receiving the full
        # rebuilt list each call is exactly what the API wants. A
        # forked session inherits its parent's context for free
        # because the host walks up the parent chain when assembling
        # the history.
        messages = _normalise_history(history)
        messages.append(
            {
                "role": "user",
                "content": [{"text": content}] if content else [{"text": ""}],
            }
        )
        return self._stream_turn(session_id, state, effective_model, messages)

    async def _stream_turn(
        self,
        session_id: str,
        state: _SessionState,
        model_id: str,
        messages: list[dict[str, Any]],
    ) -> AsyncIterator[AcpChunk]:
        runtime = self._runtime(
            self._resolve_profile(state.config.extra),
            self._resolve_region(state.config.extra),
        )
        # Tool-use loop: keep calling converse_stream while the model
        # asks for tools. The Bedrock Converse contract is: model emits
        # one or more ``contentBlockStart`` blocks of type ``toolUse``,
        # the client executes the tool, appends an assistant message
        # containing the tool_use blocks AND a user message containing
        # the matching tool_result blocks, then calls converse_stream
        # again. We cap the loop at ``max_tool_calls_per_turn`` so a
        # runaway plan cannot pin the host indefinitely.
        tool_calls_made = 0
        seq = 0
        while True:
            kwargs: dict[str, Any] = {
                "modelId": model_id,
                "messages": messages,
            }
            if state.system_prompt:
                kwargs["system"] = [{"text": state.system_prompt}]
            if state.inference_config:
                kwargs["inferenceConfig"] = state.inference_config
            tool_config = build_tool_config(state.tool_limits)
            if tool_config is not None:
                kwargs["toolConfig"] = tool_config

            try:
                response = await self._call_blocking(runtime.converse_stream, **kwargs)
            except Exception as exc:
                yield AcpChunk(
                    session_id=session_id,
                    chunk_type="error",
                    content={"error": str(exc), "type": exc.__class__.__name__},
                    seq=seq,
                )
                return

            stream = response.get("stream") if isinstance(response, dict) else None
            if stream is None:
                yield AcpChunk(
                    session_id=session_id,
                    chunk_type="error",
                    content={"error": "converse_stream response missing 'stream' iterator"},
                    seq=seq,
                )
                return

            # Per-step accumulator: rebuild the assistant message we
            # need to append to ``messages`` so the next round (if any)
            # has a faithful turn-replay. ``tool_use`` blocks are
            # captured here but we also stream a chunk to the host so
            # the UI can show progress.
            assistant_blocks: list[dict[str, Any]] = []
            current_text = ""
            current_tool: dict[str, Any] | None = None
            current_tool_input_buf = ""
            stop_reason: str | None = None
            try:
                async for event in self._aiter_blocking(stream):
                    if "contentBlockStart" in event:
                        block = event["contentBlockStart"].get("start", {})
                        tu = block.get("toolUse")
                        if tu:
                            current_tool = {
                                "toolUseId": tu.get("toolUseId"),
                                "name": tu.get("name"),
                            }
                            current_tool_input_buf = ""
                    elif "contentBlockDelta" in event:
                        delta = event["contentBlockDelta"].get("delta", {})
                        text = delta.get("text")
                        if text:
                            current_text += text
                            yield AcpChunk(
                                session_id=session_id,
                                chunk_type="text_delta",
                                content={"text": text, "model_id": model_id},
                                seq=seq,
                            )
                            seq += 1
                        tu_delta = delta.get("toolUse")
                        if tu_delta and current_tool is not None:
                            input_chunk = tu_delta.get("input")
                            if input_chunk:
                                current_tool_input_buf += input_chunk
                    elif "contentBlockStop" in event:
                        if current_tool is not None:
                            # Bedrock streams the JSON input as text; parse
                            # it once the block stops so the dict shape is
                            # what ``execute_tool`` expects.
                            import json as _json

                            try:
                                tool_input = (
                                    _json.loads(current_tool_input_buf)
                                    if current_tool_input_buf.strip()
                                    else {}
                                )
                            except _json.JSONDecodeError:
                                tool_input = {"_raw": current_tool_input_buf}
                            tool_block: dict[str, Any] = {
                                "toolUse": {
                                    "toolUseId": current_tool["toolUseId"],
                                    "name": current_tool["name"],
                                    "input": tool_input,
                                }
                            }
                            assistant_blocks.append(tool_block)
                            yield AcpChunk(
                                session_id=session_id,
                                chunk_type="tool_use",
                                content={
                                    "tool_name": current_tool["name"],
                                    "tool_use_id": current_tool["toolUseId"],
                                    "input": tool_input,
                                    "model_id": model_id,
                                },
                                seq=seq,
                            )
                            seq += 1
                            current_tool = None
                            current_tool_input_buf = ""
                        elif current_text:
                            assistant_blocks.append({"text": current_text})
                            current_text = ""
                    elif "messageStop" in event:
                        stop_reason = event["messageStop"].get("stopReason")
                        # Drain any trailing text not yet flushed at
                        # contentBlockStop boundaries.
                        if current_text:
                            assistant_blocks.append({"text": current_text})
                            current_text = ""
                    elif "internalServerException" in event or "modelStreamErrorException" in event:
                        error_event = event.get("internalServerException") or event.get(
                            "modelStreamErrorException"
                        )
                        yield AcpChunk(
                            session_id=session_id,
                            chunk_type="error",
                            content={
                                "error": str(error_event),
                                "type": "BedrockStreamError",
                            },
                            seq=seq,
                        )
                        return
            except Exception as exc:
                yield AcpChunk(
                    session_id=session_id,
                    chunk_type="error",
                    content={"error": str(exc), "type": exc.__class__.__name__},
                    seq=seq,
                )
                return

            # Decide whether we owe the model another round. ``tool_use``
            # is the only stop reason where we keep going.
            tool_blocks = [b for b in assistant_blocks if "toolUse" in b]
            if stop_reason != "tool_use" or not tool_blocks:
                break

            if tool_calls_made >= state.tool_limits.max_tool_calls_per_turn:
                yield AcpChunk(
                    session_id=session_id,
                    chunk_type="error",
                    content={
                        "error": (
                            "tool-use cap reached "
                            f"({state.tool_limits.max_tool_calls_per_turn}); "
                            "summarising without further tool calls"
                        ),
                        "type": "BedrockToolCapExceeded",
                    },
                    seq=seq,
                )
                seq += 1
                break

            # Execute every tool block this round emitted, build the
            # matching toolResult message, then loop. Tool execution is
            # synchronous and quick (HTTP / file read with caps), so we
            # stay in the asyncio loop via ``run_in_executor`` for the
            # blocking parts inside ``execute_tool``.
            messages.append({"role": "assistant", "content": assistant_blocks})
            tool_result_blocks: list[dict[str, Any]] = []
            for block in tool_blocks:
                tu = block["toolUse"]
                result = await self._call_blocking_callable(
                    execute_tool,
                    tu["name"],
                    tu.get("input") or {},
                    cwd=state.config.cwd,
                    limits=state.tool_limits,
                )
                tool_result_blocks.append(
                    {
                        "toolResult": {
                            "toolUseId": tu["toolUseId"],
                            "content": result.get("content") or [{"text": ""}],
                            "status": result.get("status", "success"),
                        }
                    }
                )
                yield AcpChunk(
                    session_id=session_id,
                    chunk_type="tool_result",
                    content={
                        "tool_name": tu["name"],
                        "tool_use_id": tu["toolUseId"],
                        "status": result.get("status", "success"),
                        "model_id": model_id,
                    },
                    seq=seq,
                )
                seq += 1
                tool_calls_made += 1
            messages.append({"role": "user", "content": tool_result_blocks})

        # No buffer mutation: the host is the single source of truth
        # for conversation state and rebuilds the message list every
        # turn. Keeping the adapter stateless is what lets a forked
        # session inherit its parent's context for free.
        yield AcpChunk(
            session_id=session_id,
            chunk_type="end_turn",
            content={"model_id": model_id},
            seq=seq,
        )

    async def cancel(self, session_id: str) -> None:
        # boto3's converse_stream does not expose a direct cancel hook;
        # we mark the session inactive so any in-flight loop sees the
        # change on its next event and gives up.
        state = self._sessions.get(session_id)
        if state is not None:
            state.active = False

    async def close(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    async def handle_permission(
        self,
        request: PermissionRequest,
        granted: bool,
    ) -> None:
        # The built-in tools (web_fetch, file_read) execute under hard
        # byte / time / call-count caps, so the adapter does not raise
        # PermissionRequest events: there is nothing for the host to
        # approve mid-turn. If a future release adds a side-effecting
        # tool, this is where the gate would land.
        return None

    def normalize(self, raw_line: str, seq: int) -> list[NormalizedTurn]:
        # The Bedrock adapter does not emit JSONL transcripts; the host
        # captures conversation state directly from AcpChunks.
        return []

    def resume_args(self, frozen_jsonl_path: str) -> tuple[str, ...]:
        # No CLI to resume; the messages list is the only state.
        return ()

    # -- catalogue + default ----------------------------------------------

    async def list_models(self, filter: ModelFilter | None = None) -> tuple[ModelInfo, ...]:
        # We pull the catalogue from whatever ``BackendConfig`` was last
        # initialised; this keeps the API ergonomic when callers ask
        # ``session.list_models()`` without re-supplying credentials.
        # If no session has been initialised yet we fall back to env
        # creds so the picker UI can populate from a cold start.
        config = self._latest_config()
        models = await self._fetch_catalogue(config)
        if filter is None:
            return models
        return filter.apply(models)

    @property
    def default_model_id(self) -> str | None:
        # The catalogue fetch happens inside ``initialize`` /
        # ``_pick_default_model`` so this property only reports the
        # cached fallback once the adapter has had a chance to warm up.
        return self._fallback_default

    # -- internals --------------------------------------------------------

    def _latest_config(self) -> BackendConfig:
        for state in reversed(self._sessions.values()):
            return state.config
        return BackendConfig(backend=self.backend_name, cwd=".")

    async def _fetch_catalogue(self, config: BackendConfig) -> tuple[ModelInfo, ...]:
        profile = self._resolve_profile(config.extra)
        region = self._resolve_region(config.extra)
        cached = self._catalogue_cache.get((profile, region))
        if cached is not None:
            return cached
        try:
            client = self._control(profile, region)
            fm_payload = await self._call_blocking(client.list_foundation_models)
        except Exception:
            logger.exception("bedrock list_foundation_models failed")
            return ()

        # Inference-profile-only models cannot be invoked with their bare
        # foundation-model id; ``converse_stream`` rejects the call with
        # ``ValidationException``. We fetch the inference-profile catalogue
        # too and substitute the appropriate profile id (e.g.
        # ``us.anthropic.claude-opus-4-7-v1:0``) for those models so the
        # picker only ever surfaces invocable identifiers.
        try:
            ip_payload = await self._call_blocking(client.list_inference_profiles)
        except Exception:
            # Treat profiles as unavailable if the call fails -- worst
            # case the picker drops a few IP-only entries, which is what
            # the user sees today before this fix.
            logger.exception("bedrock list_inference_profiles failed")
            ip_payload = {"inferenceProfileSummaries": []}

        profiles_by_fm: dict[str, str] = self._index_inference_profiles(
            ip_payload.get("inferenceProfileSummaries") if isinstance(ip_payload, Mapping) else []
        )

        summaries = fm_payload.get("modelSummaries", []) if isinstance(fm_payload, Mapping) else []
        models: list[ModelInfo] = []
        for entry in summaries:
            model_id = entry.get("modelId")
            if not model_id:
                continue
            output_modalities = entry.get("outputModalities") or []
            if "TEXT" not in output_modalities:
                continue
            inference_types = set(entry.get("inferenceTypesSupported") or ())
            if not (inference_types & {"ON_DEMAND", "INFERENCE_PROFILE"}):
                continue
            if not entry.get("responseStreamingSupported", True):
                continue

            invocable_id = model_id
            via_profile = False
            if "ON_DEMAND" not in inference_types:
                # Inference-profile-only: rewrite the id to the matching
                # profile (or drop the entry if none exists for this
                # region).
                profile_id = profiles_by_fm.get(model_id)
                if profile_id is None:
                    continue
                invocable_id = profile_id
                via_profile = True

            family = self._infer_family(model_id, entry.get("providerName"))
            display_name = entry.get("modelName") or model_id
            models.append(
                ModelInfo(
                    id=invocable_id,
                    name=display_name,
                    family=family,
                    provider=entry.get("providerName"),
                    description=None,
                    extra={
                        "inferenceTypesSupported": tuple(sorted(inference_types)),
                        "outputModalities": tuple(output_modalities),
                        "foundation_model_id": model_id,
                        "via_inference_profile": via_profile,
                    },
                )
            )
        # Sort by family then name for a stable picker order.
        models.sort(key=lambda m: ((m.family or "zzz"), m.name.lower()))
        result = tuple(models)
        self._catalogue_cache[(profile, region)] = result
        return result

    @staticmethod
    def _index_inference_profiles(summaries: object) -> dict[str, str]:
        """Build ``foundation_model_id -> profile_id`` from list_inference_profiles.

        A foundation model can have multiple matching profiles (regional
        ``us.*`` / ``eu.*``, plus a ``global.*`` aggregate). We prefer
        the regional profile keyed by the operator's configured region
        when one is obvious, otherwise fall back to the first profile
        whose models list contains the foundation model. Only one entry
        per foundation model is kept; the picker's filter input lets
        the user search by the human-friendly model name regardless.
        """

        out: dict[str, str] = {}
        if not isinstance(summaries, list):
            return out
        # Two-pass: first record any regional ``us.*`` mapping, then
        # fill in remaining gaps with whatever else is available.
        for prefer_prefix in (("us.", "eu.", "apac."), ("global.",), ()):
            for entry in summaries:
                if not isinstance(entry, Mapping):
                    continue
                profile_id = entry.get("inferenceProfileId")
                if not profile_id:
                    continue
                if prefer_prefix and not any(profile_id.startswith(p) for p in prefer_prefix):
                    continue
                for model_ref in entry.get("models") or ():
                    if not isinstance(model_ref, Mapping):
                        continue
                    arn = model_ref.get("modelArn", "")
                    # Foundation-model arns end with the literal model id
                    # after the trailing ``foundation-model/`` segment.
                    marker = "foundation-model/"
                    idx = arn.rfind(marker)
                    if idx == -1:
                        continue
                    fm_id = arn[idx + len(marker) :]
                    if fm_id and fm_id not in out:
                        out[fm_id] = profile_id
        return out

    @staticmethod
    def _infer_family(model_id: str, provider: object) -> str | None:
        token = model_id.lower()
        if "claude" in token:
            return "claude"
        if token.startswith("amazon.nova") or "nova" in token:
            return "nova"
        if "llama" in token:
            return "llama"
        if "mistral" in token or "mixtral" in token:
            return "mistral"
        if "titan" in token:
            return "titan"
        prov = _coerce_str(provider)
        return prov.lower() if prov else None

    async def _pick_default_model(self, config: BackendConfig) -> str | None:
        catalogue = await self._fetch_catalogue(config)
        preferred_substring = _coerce_str(config.extra.get("default_model_substring"))
        order = ((preferred_substring,) if preferred_substring else ()) + _DEFAULT_PREFERENCE
        for substring in order:
            for model in catalogue:
                blob = f"{model.id} {model.name}".lower()
                if substring.lower() in blob:
                    self._fallback_default = model.id
                    return model.id
        if catalogue:
            self._fallback_default = catalogue[0].id
            return catalogue[0].id
        return None

    @staticmethod
    async def _call_blocking(fn: Any, **kwargs: Any) -> Any:
        """Run a blocking boto3 call inside the asyncio default executor."""

        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(**kwargs))

    @staticmethod
    async def _call_blocking_callable(fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Run an arbitrary blocking callable (positional + keyword args).

        Tool implementations live outside boto3 and need positional
        arguments, so we offer a sister helper rather than overloading
        :meth:`_call_blocking` (whose ``**kwargs``-only signature pins
        boto3's named-arg API).
        """

        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    @staticmethod
    async def _aiter_blocking(stream: Any) -> AsyncIterator[Any]:
        """Yield items from a blocking iterator without stalling the loop."""

        import asyncio

        loop = asyncio.get_running_loop()
        sentinel = object()

        def _next(it: Any) -> Any:
            try:
                return next(it)
            except StopIteration:
                return sentinel

        iterator = iter(stream)
        while True:
            value = await loop.run_in_executor(None, _next, iterator)
            if value is sentinel:
                return
            yield value


register_backend("bedrock", BedrockBackend)
