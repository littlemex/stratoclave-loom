# stratoclave-loom — project status

**Last updated**: 2026-05-22 (kiro_code adapter added)
**Project started**: 2026-05-21

## Overall progress

### v0.1 walking skeleton — implementation status

| Module | Status | Tests | Notes |
|---|---|---|---|
| `core.types` (`AcpChunk` / `BackendConfig` / `NormalizedTurn` / `PermissionRequest`) | done | indirect | frozen dataclasses |
| `core.errors` (`LoomError` hierarchy) | done | indirect | 5 error classes |
| `core.backend` (`AgentBackend` ABC) | done | indirect | 7 abstract methods |
| `core.session` (`AgentSession`) | done | done | async context manager |
| `config` (env loader, `LoomSettings`) | done | done | defaults + env override |
| `transport.stdio` (`StdioTransport`) | done | done | spawn / send / receive / cancel / close |
| `runtime.process_pool` (`ProcessPool`) | done | done | semaphore-based lease |
| `adapters._registry` (`register_backend` / `get_backend`) | done | done | global dict |
| `adapters.mock` (`MockBackend`) | done | done | char-wise text_delta + end_turn + cancel |
| **`adapters.claude_code` (`ClaudeCodeBackend`)** | **done** | **done** | spawns `claude --print --output-format stream-json`, captures CLI session_id from `system/init`, propagates to `--resume` on subsequent turns. End-to-end verified against `claude` 2.1.145 |
| **`adapters.kiro_code` (`KiroCodeBackend`)** | **done** | **done** | drives `kiro-cli acp` via JSON-RPC 2.0 (initialize → session/new → session/prompt). Translates `agent_message_chunk` / `tool_call` / `tool_call_update` into AcpChunks; cancel via `session/cancel`. End-to-end verified against `kiro-cli` 2.4.0 |
| `cli` (`stratoclave-loom run/list-backends`) | done | done | argparse, JSONL stdout |
| `examples/quickstart.py` | done | manual | prefers `claude_code`, then `kiro_code`, falls back to `mock` |

### Quality gates

| Item | Result |
|---|---|
| pytest | 37 passed (27 base + 10 kiro_code wire-level) |
| ruff lint | clean |
| ruff format | clean |
| mypy strict | clean |
| Python | verified on 3.12, CI runs 3.11 + 3.12 |
| Live `claude` CLI | end-to-end verified (text_delta → end_turn) |
| Live `kiro-cli` | end-to-end verified (text_delta → end_turn against `kiro-cli acp` 2.4.0) |

### Cross-OSS integration

| Counterpart | Status | Notes |
|---|---|---|
| stratoclave (Bedrock proxy) | not coupled | env passthrough only; no direct import |
| stratoclave-distill | not started | separate OSS, future work |
| stratoclave-atelier | not started | separate OSS, future work |
| claude-capture (legacy) | not started | replaced after atelier ships |

## Completed work

### 2026-05-22 — Kiro Code adapter (ACP backend)

- New `KiroCodeBackend` (`src/stratoclave_loom/adapters/kiro_code.py`, ~590 LoC) drives `kiro-cli acp` over JSON-RPC 2.0 on stdio.
- Handshake: `initialize` (protocolVersion 1) → `session/new` (or `session/load` when `extra['acp_session_id']` is supplied) → `session/prompt`. The adapter holds one long-lived subprocess per loom session and demultiplexes responses with a reader task.
- Stream translation: `session/update` notifications map to AcpChunks — `agent_message_chunk` → `text_delta`, `agent_thought_chunk` → `thought`, `tool_call` → `tool_use(start)`, `tool_call_update` → `tool_use(update)` while running and `tool_result` on `completed`/`failed`. Terminal `{stopReason}` from the prompt response becomes `end_turn` (or `error` for JSON-RPC errors).
- Cancel: emits `session/cancel` notification; the agent resolves the in-flight prompt with `stopReason:"cancelled"`.
- Tool name normalization: `shell→shell.run`, `read→file.read`, `write→file.write`, `grep→file.grep`, `glob→file.glob`, `code→code.intel`, `use_aws→aws.cli`, `web_fetch→web.fetch`, `web_search→web.search`. Unknown names pass through unchanged.
- argv knobs surfaced via `BackendConfig.extra`: `agent`, `model`, `agent_engine` (`v1`/`v2`/`kas`), `trust_all_tools`, `trust_tools`, `token_path`, `mcp_servers`, `extra_cli_args`. Resolution of the `kiro-cli` binary follows `extra['kiro_cli']` → `$STRATOCLAVE_LOOM_KIRO_CLI` → `shutil.which('kiro-cli')`.
- `handle_permission` raises (no runtime permission routing yet); permissions are pre-approved at spawn via `trust_all_tools` / `trust_tools`. `resume_from_jsonl` is rejected with a clear error pointing to `extra['acp_session_id']`.
- 10 wire-level tests (`tests/adapters/test_kiro_code_wire.py`) using a Python ACP stub (`tests/adapters/_kiro_stub.py`): text deltas, tool_call + tool_call_update, cancel mid-turn, idempotent close, env-var resolution, argv carries `--trust-tools`/`--agent-engine`/`--model`, `session/load` with pre-existing ACP session id, `resume_from_jsonl` rejection, `handle_permission` raises.
- Live test against `kiro-cli` 2.4.0: prompt "Reply with the single word OK and nothing else." → `text_delta` ("OK") + `end_turn`.

### 2026-05-22 — wire-level Claude Code adapter

- `ClaudeCodeBackend.send_message` spawns `claude --print --output-format stream-json --input-format stream-json --include-partial-messages --verbose` and translates each line into an `AcpChunk`.
- Translates: `system/init` (captures CLI session id for resume), `stream_event/content_block_delta/text_delta` → `text_delta` chunk, `stream_event/content_block_start/tool_use` → `tool_use` chunk, `stream_event/content_block_delta/input_json_delta` → `tool_use` partial chunk, `user/tool_result` → `tool_result` chunk, `assistant/thinking` → `thought` chunk, `result` → `end_turn` or `error` chunk.
- Tool names normalized: `Bash` → `shell.run`, `Read` → `file.read`, `Write` → `file.write`, `Edit` → `file.edit`, `Glob` → `file.glob`, `Grep` → `file.grep`. Unknown names pass through unchanged.
- `cancel` routes to `StdioTransport.cancel()` (SIGINT → SIGTERM → SIGKILL ladder, ms grace from `STRATOCLAVE_LOOM_CANCEL_GRACE_MS`).
- `close` is idempotent and tears down the in-flight transport if any.
- `handle_permission` raises `AdapterError`: `claude --print` has no runtime permission callback; permissions must be configured at spawn via `BackendConfig.allowed_tools` / `extra['permission_mode']`.
- Resolves the `claude` executable via, in order: `BackendConfig.extra['claude_cli']` → `$STRATOCLAVE_LOOM_CLAUDE_CLI` → `shutil.which('claude')`.
- Subsequent turns within one `AgentSession` resume by passing `--resume <captured-session-id>`.
- Added 9 wire-level tests using a Python stub binary (`tests/adapters/_claude_stub.py`): text deltas, tool_use translation, error result, cancel-mid-turn, idempotent close, env-var resolution, resume id propagation.
- Added `examples/quickstart.py`. Live test against `claude` 2.1.145: prompt "reply OK and nothing else" → 2 text deltas + end_turn (~2.1 s, $0.167).

### 2026-05-21 — initial walking skeleton

- Confirmed 4-OSS series design in `docs/DESIGN.md` (1014 lines).
- Repo skeleton: LICENSE / README / CONTRIBUTING / SECURITY / CODE_OF_CONDUCT / .gitignore.
- `pyproject.toml` (Python 3.11+ / Apache-2.0 / pytest + ruff + mypy + coverage).
- `core` / `config` / `transport` / `adapters` / `runtime` / `cli` modules.
- Mock backend and Claude Code skeleton (normalize-only at the time).
- 18 unit tests, CLI smoke test, StdioTransport round-trip.
- GitHub Actions CI workflow.
- Three required docs (this file is one).

## Technical highlights

- **Pure Python** — runtime dependencies = stdlib only.
- **mypy strict clean** — zero typing escapes.
- **AgentBackend ABC** — 7 methods covering the CLI surface area.
- **stdio JSON-RPC framing** — subprocess lifecycle is `start / send / receive / cancel / close`.
- **Env-only stratoclave coupling** — loom never imports stratoclave; the orchestrator passes proxy env vars at spawn.
- **Tool name normalization** — both directions (CLI → normalized for output, normalized → CLI for `--allowed-tools`).

## Outstanding / next up

### v0.1 — remaining

| Priority | Task |
|---|---|
| Medium | `pre-commit` setup |
| Medium | Optional `pytest -m e2e` job that drives the real `claude` CLI |
| Low | `StdioTransport.receive` back-pressure test |
| Low | Translate the rest of `docs/` to English (DESIGN.md / GETTING_STARTED.md / PROJECT_RULES.md) |

### v0.2

- OpenCode adapter (after PoC) using its native ACP server.
- `ProcessPool` priority / queueing.
- `pyproject.toml` Python 3.13 classifier.

### v0.3

- Codex adapter (after PoC).
- Adapter-side enforcement of `allowed_tools` for Kiro (today the adapter only forwards spawn-time hints).
- Runtime permission routing for `kiro_code` — wire `session/request_permission` (ACP) into `PermissionRequest` / `handle_permission`.
- `resume_from_jsonl` support across all adapters.

### v1.0

- Public API freeze (semver 1.x).
- Performance work — subprocess pool reuse, stdio zero-copy.

## Team

| Role | Agent | Status | Current task |
|---|---|---|---|
| Owner | littlemex | active | design / PR review |
| Implementer | Claude Code | active | v0.1 polish (pre-commit, e2e harness) |

## Next steps

In priority order:

1. **Pre-commit** — wire ruff + mypy + pytest into the local pre-commit flow.
2. **E2E test harness** — opt-in pytest mark that exercises the real `claude` CLI.
3. **stratoclave-distill bootstrapping** — start the next OSS now that loom is real (DESIGN.md is already aligned).
4. **English doc pass** — DESIGN.md, GETTING_STARTED.md, PROJECT_RULES.md.

## Links

- [GETTING_STARTED.md](./GETTING_STARTED.md)
- [PROJECT_RULES.md](./PROJECT_RULES.md)
- [DESIGN.md](./DESIGN.md)
- [README.md](../README.md)
- [examples/quickstart.py](../examples/quickstart.py)
