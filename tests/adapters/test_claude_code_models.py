"""Tests for the claude_code adapter's model picker surface.

Pinning the curated catalogue + the ``--model`` argv plumbing so a
future refactor cannot silently strip them. The catalogue itself is
loaded from a packaged JSON file so operators can append new model
ids without an atelier restart; the loader contract lives in
:mod:`stratoclave_loom.core.catalogue` and has its own tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from stratoclave_loom import ModelFilter, get_backend
from stratoclave_loom.adapters.claude_code import (
    _DEFAULT_MODELS_FILE,
    _ENV_MODELS_FILE,
    _EXTRA_MODELS_KEY,
    _FALLBACK_MODELS,
    ClaudeCodeBackend,
    _load_curated_models,
    _SessionState,
)


@pytest.fixture
def backend() -> ClaudeCodeBackend:
    instance = get_backend("claude_code")
    assert isinstance(instance, ClaudeCodeBackend)
    return instance


async def test_packaged_catalogue_lists_at_least_the_three_aliases(
    backend: ClaudeCodeBackend,
) -> None:
    models = await backend.list_models()
    ids = [m.id for m in models]
    # ``haiku`` / ``sonnet`` / ``opus`` are the CLI aliases and must
    # always be available so the picker has a sensible default tier
    # set even if the JSON is trimmed down.
    for required in ("haiku", "sonnet", "opus"):
        assert required in ids, f"alias {required!r} missing from catalogue"
    assert {m.provider for m in models} == {"anthropic"}
    assert {m.family for m in models} == {"claude"}


async def test_substring_filter_narrows_catalogue(
    backend: ClaudeCodeBackend,
) -> None:
    only_opus = await backend.list_models(ModelFilter(substring="opus"))
    # The packaged catalogue ships several Opus entries (alias +
    # specific versions). All of them must survive a positive
    # substring filter; no false positives leak through.
    ids = [m.id for m in only_opus]
    assert "opus" in ids
    assert all("opus" in m.id.lower() or "opus" in m.name.lower() for m in only_opus)


async def test_default_model_is_none_so_cli_owns_it(
    backend: ClaudeCodeBackend,
) -> None:
    assert backend.default_model_id is None


def test_build_argv_includes_model_when_set() -> None:
    from stratoclave_loom.core.types import BackendConfig

    backend = ClaudeCodeBackend()
    state = _SessionState(BackendConfig(backend="claude_code", cwd="."), "claude")
    state.current_model = "sonnet"
    argv = backend._build_argv(state)
    assert "--model" in argv
    idx = argv.index("--model")
    assert argv[idx + 1] == "sonnet"


def test_build_argv_omits_model_flag_when_unset() -> None:
    from stratoclave_loom.core.types import BackendConfig

    backend = ClaudeCodeBackend()
    state = _SessionState(BackendConfig(backend="claude_code", cwd="."), "claude")
    argv = backend._build_argv(state)
    assert "--model" not in argv


def test_packaged_catalogue_file_ships_with_the_wheel() -> None:
    """The default catalogue file must exist next to the source so the
    wheel build picks it up. Keeping this asserted here means a refactor
    that moves the data dir surfaces immediately rather than after an
    install at the customer's site."""

    assert _DEFAULT_MODELS_FILE.is_file(), f"packaged catalogue missing at {_DEFAULT_MODELS_FILE}"
    payload = json.loads(_DEFAULT_MODELS_FILE.read_text(encoding="utf-8"))
    # Either shape (wrapped or bare list) is OK.
    items = payload["models"] if isinstance(payload, dict) else payload
    assert isinstance(items, list) and len(items) >= 3, (
        "packaged catalogue must list at least the three CLI aliases"
    )


def test_loader_falls_back_when_file_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the JSON file is replaced with a non-existent path, the
    adapter must surface a non-empty fallback so the picker never
    looks empty for a configured claude_code backend."""

    monkeypatch.setenv(_ENV_MODELS_FILE, "/tmp/this/path/does/not/exist.json")
    out = _load_curated_models(extra=None)
    assert out == _FALLBACK_MODELS


def test_loader_picks_up_external_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Operators can override the catalogue at run time via the env
    var so a new model id can land before a loom release. Pin the
    override path so we don't accidentally fall back to the
    packaged JSON when the env var is honoured."""

    custom = tmp_path / "custom.json"
    custom.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "custom-mini",
                        "name": "Custom Mini",
                        "family": "custom",
                        "provider": "test",
                    }
                ]
            }
        )
    )
    monkeypatch.setenv(_ENV_MODELS_FILE, str(custom))
    out = _load_curated_models(extra=None)
    assert [m.id for m in out] == ["custom-mini"]


def test_loader_extra_key_wins_over_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``BackendConfig.extra['model_catalogue_path']`` is the highest-
    precedence override. Two files: env points at one, extra at the
    other; extra must win."""

    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps([{"id": "from-env", "name": "Env"}]))
    extra_file = tmp_path / "extra.json"
    extra_file.write_text(json.dumps([{"id": "from-extra", "name": "Extra"}]))
    monkeypatch.setenv(_ENV_MODELS_FILE, str(env_file))
    out = _load_curated_models(extra={_EXTRA_MODELS_KEY: str(extra_file)})
    assert [m.id for m in out] == ["from-extra"]


# ---------------------------------------------------------------------------
# --dangerously-skip-permissions default
# ---------------------------------------------------------------------------


def test_build_argv_defaults_to_dangerously_skip_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin: a default-config session adds ``--dangerously-skip-permissions``.

    Each ``send_message`` spawns a fresh subprocess, so without this
    flag the CLI's interactive permission prompt would abort turns
    silently the moment Claude tried to call ``gh`` / ``WebFetch`` /
    similar. Atelier already trusts the configured cwd, so defaulting
    the skip ON matches the operator's expectation."""

    monkeypatch.delenv("STRATOCLAVE_LOOM_CLAUDE_DANGEROUSLY_SKIP", raising=False)
    from stratoclave_loom.core.types import BackendConfig

    backend = ClaudeCodeBackend()
    state = _SessionState(BackendConfig(backend="claude_code", cwd="."), "claude")
    argv = backend._build_argv(state)
    assert "--dangerously-skip-permissions" in argv


def test_extra_can_opt_out_of_skip_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Security-sensitive deployments opt out via the extras flag.
    The argv must NOT contain ``--dangerously-skip-permissions`` when
    the operator says no."""

    monkeypatch.delenv("STRATOCLAVE_LOOM_CLAUDE_DANGEROUSLY_SKIP", raising=False)
    from stratoclave_loom.core.types import BackendConfig

    backend = ClaudeCodeBackend()
    state = _SessionState(
        BackendConfig(
            backend="claude_code",
            cwd=".",
            extra={"dangerously_skip_permissions": False},
        ),
        "claude",
    )
    argv = backend._build_argv(state)
    assert "--dangerously-skip-permissions" not in argv


def test_explicit_permission_mode_suppresses_default_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the operator pins a specific ``--permission-mode`` (e.g.
    ``acceptEdits``), the default skip flag stays OFF -- mixing
    ``--permission-mode`` and ``--dangerously-skip-permissions`` is
    an obvious user-intent conflict."""

    monkeypatch.delenv("STRATOCLAVE_LOOM_CLAUDE_DANGEROUSLY_SKIP", raising=False)
    from stratoclave_loom.core.types import BackendConfig

    backend = ClaudeCodeBackend()
    state = _SessionState(
        BackendConfig(
            backend="claude_code",
            cwd=".",
            extra={"permission_mode": "acceptEdits"},
        ),
        "claude",
    )
    argv = backend._build_argv(state)
    assert "--dangerously-skip-permissions" not in argv
    assert "--permission-mode" in argv
    assert "acceptEdits" in argv


def test_env_var_can_disable_default_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STRATOCLAVE_LOOM_CLAUDE_DANGEROUSLY_SKIP", "0")
    from stratoclave_loom.core.types import BackendConfig

    backend = ClaudeCodeBackend()
    state = _SessionState(BackendConfig(backend="claude_code", cwd="."), "claude")
    argv = backend._build_argv(state)
    assert "--dangerously-skip-permissions" not in argv
