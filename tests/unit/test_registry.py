"""Tests for the backend registry."""

from __future__ import annotations

import pytest

from stratoclave_loom import BackendNotFoundError, get_backend, list_backends


def test_built_in_backends_are_registered() -> None:
    names = list_backends()
    assert "mock" in names
    assert "claude_code" in names


def test_unknown_backend_raises() -> None:
    with pytest.raises(BackendNotFoundError):
        get_backend("__does_not_exist__")
