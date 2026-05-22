<div align="center">

# stratoclave-loom

**A Pure Python abstraction over coding agent CLIs (Claude Code, OpenCode, Kiro, Codex, ...) speaking ACP.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](./LICENSE)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-orange.svg)](#project-status)

*"One protocol, every coding agent."*

</div>

---

## What is stratoclave-loom?

stratoclave-loom is a Python library that wraps any compatible coding agent
CLI behind a single, ACP-flavoured interface. You write your application
once and decide at runtime which agent backend should run the turn:
**Claude Code**, **OpenCode**, **Kiro**, **Codex**, or any future addition.

The library is intentionally narrow:

- It **spawns one agent process per session** and brokers stdio JSON-RPC.
- It **normalises streaming chunks** (`AcpChunk`) so callers do not have to
  handle agent-specific delta formats.
- It **passes environment variables through** (e.g. `ANTHROPIC_BASE_URL`,
  `ANTHROPIC_AUTH_TOKEN`) so you can route traffic via
  [stratoclave](https://github.com/littlemex/stratoclave) — but it does not
  itself depend on stratoclave.
- It **does not** manage groups, version history, embeddings, retrieval, or
  cross-session orchestration. Those concerns belong to higher layers
  (see [Series](#series)).

```text
        Your application
              |
              v
    +---------------------+
    |  stratoclave-loom   |  <-- you are here
    |  AgentBackend ABC   |
    +-----+---------------+
          | subprocess (stdio)
          v
    Claude Code | OpenCode | Kiro | Codex
```

## Project status

**Alpha — v0.1 in active development.** The Python API and CLI surface may
change between commits. Pin to a specific commit if you depend on this for
something important.

| Adapter      | Status                | Notes                              |
|--------------|-----------------------|-------------------------------------|
| Claude Code  | v0.1 (live)           | Reference adapter (`claude --print` stream-json) |
| Kiro CLI     | v0.1 (live)           | `kiro-cli acp` — native ACP JSON-RPC over stdio |
| OpenCode     | v0.2 (planned)        | Pending PoC of native ACP           |
| Codex        | v0.3 (planned)        | OpenAI streaming → AcpChunk         |
| Mock         | v0.1                  | For tests and getting-started demos |

## Installation

```bash
pip install stratoclave-loom
```

Editable install for development:

```bash
git clone https://github.com/littlemex/stratoclave-loom.git
cd stratoclave-loom
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Quick start

```python
import asyncio
from stratoclave_loom import BackendConfig, create_session


async def main() -> None:
    cfg = BackendConfig(
        backend="mock",          # swap to "claude_code" once installed
        cwd=".",
        env={},                  # passthrough; e.g. ANTHROPIC_BASE_URL
    )
    async with create_session(cfg) as session:
        async for chunk in session.send_message("hello"):
            print(chunk.chunk_type, chunk.content)


asyncio.run(main())
```

CLI:

```bash
stratoclave-loom list-backends
stratoclave-loom run --backend mock --message "hello"
```

## Configuration

stratoclave-loom never hard-codes paths, URLs, or model names. Configuration
flows through `BackendConfig` and (optionally) the following environment
variables:

| Variable                    | Purpose                                         |
|-----------------------------|-------------------------------------------------|
| `STRATOCLAVE_LOOM_LOG_LEVEL`| Library log level (`DEBUG`/`INFO`/...)         |
| `STRATOCLAVE_LOOM_BUFFER`   | Internal stdio buffer ceiling (bytes)           |
| `STRATOCLAVE_LOOM_CANCEL_GRACE_MS` | Grace period before SIGTERM after SIGINT |
| `ANTHROPIC_BASE_URL`        | Forwarded to Claude Code subprocess             |
| `ANTHROPIC_AUTH_TOKEN`      | Forwarded to Claude Code subprocess             |
| `STRATOCLAVE_LOOM_CLAUDE_CLI` | Override the `claude` executable path          |
| `STRATOCLAVE_LOOM_KIRO_CLI` | Override the `kiro-cli` executable path         |

`BackendConfig.env` always takes precedence over the parent process
environment for child subprocesses.

## Series

stratoclave-loom is part of the stratoclave family of OSS projects:

| Project                     | Role                                                       |
|-----------------------------|------------------------------------------------------------|
| [stratoclave](https://github.com/littlemex/stratoclave) | Tenant-aware Bedrock proxy (auth, credit, audit). |
| **stratoclave-loom**        | **Single-agent execution abstraction. (this repo)**         |
| stratoclave-distill         | Session distillation, search, and learnings (planned).      |
| stratoclave-atelier         | Web UI, version DB, cross-session orchestration (planned).  |

stratoclave-loom is independent: it has no compile-time or runtime
dependency on the other projects. Use it standalone, or compose it with the
others.

## Documentation

- [`docs/DESIGN.md`](./docs/DESIGN.md) — full series design (loom +
  distill + atelier + stratoclave).
- [`docs/GETTING_STARTED.md`](./docs/GETTING_STARTED.md) — install and run.
- [`docs/PROJECT_STATUS.md`](./docs/PROJECT_STATUS.md) — current state.
- [`docs/PROJECT_RULES.md`](./docs/PROJECT_RULES.md) — project-specific rules.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md). Security issues belong in
[SECURITY.md](./SECURITY.md), not in public issues.

## License

Apache 2.0 — see [LICENSE](./LICENSE).
