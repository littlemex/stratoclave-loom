"""Tests for environment-driven settings."""

from __future__ import annotations

import pytest

from stratoclave_loom.config import load_settings, reset_settings_cache


def test_defaults_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STRATOCLAVE_LOOM_LOG_LEVEL", raising=False)
    monkeypatch.delenv("STRATOCLAVE_LOOM_BUFFER", raising=False)
    monkeypatch.delenv("STRATOCLAVE_LOOM_CANCEL_GRACE_MS", raising=False)
    reset_settings_cache()
    s = load_settings()
    assert s.log_level == "INFO"
    assert s.buffer_bytes == 64 * 1024
    assert s.cancel_grace_ms == 500


def test_invalid_int_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATOCLAVE_LOOM_BUFFER", "not-an-int")
    with pytest.raises(ValueError):
        load_settings()


def test_env_overrides_are_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STRATOCLAVE_LOOM_LOG_LEVEL", "debug")
    monkeypatch.setenv("STRATOCLAVE_LOOM_BUFFER", "1024")
    monkeypatch.setenv("STRATOCLAVE_LOOM_CANCEL_GRACE_MS", "200")
    reset_settings_cache()
    s = load_settings()
    assert s.log_level == "DEBUG"
    assert s.buffer_bytes == 1024
    assert s.cancel_grace_ms == 200
