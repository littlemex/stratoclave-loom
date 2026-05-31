"""Tests for the shared :class:`ModelFilter` primitive."""

from __future__ import annotations

from stratoclave_loom import ModelFilter, ModelInfo


def _model(**kwargs: object) -> ModelInfo:
    base = {
        "id": "x",
        "name": "X",
        "family": None,
        "provider": None,
    }
    base.update(kwargs)
    return ModelInfo(**base)  # type: ignore[arg-type]


def test_empty_filter_matches_everything() -> None:
    f = ModelFilter()
    assert f.matches(_model(id="anthropic.claude", name="Claude"))
    assert f.matches(_model(id="amazon.nova", name="Nova"))


def test_substring_searches_id_name_family_and_provider() -> None:
    f = ModelFilter(substring="opus 4.7")
    assert f.matches(_model(id="anthropic.claude-opus-4-7-v1:0", name="Claude Opus 4.7"))
    assert not f.matches(_model(id="anthropic.claude-sonnet-4", name="Claude Sonnet 4"))


def test_substring_is_case_insensitive() -> None:
    f = ModelFilter(substring="LLAMA")
    assert f.matches(_model(id="meta.llama3-70b", name="Llama 3 70B", family="llama"))


def test_family_constraint_is_exact() -> None:
    f = ModelFilter(family="claude")
    assert f.matches(_model(id="x", name="X", family="claude"))
    assert not f.matches(_model(id="x", name="X", family="nova"))
    # Missing family field never matches a constrained filter.
    assert not f.matches(_model(id="x", name="X", family=None))


def test_provider_constraint_is_exact_and_case_insensitive() -> None:
    f = ModelFilter(provider="Anthropic")
    assert f.matches(_model(id="x", name="X", provider="anthropic"))
    assert not f.matches(_model(id="x", name="X", provider="amazon"))


def test_filter_apply_preserves_input_order() -> None:
    models = (
        _model(id="a", name="A claude", family="claude"),
        _model(id="b", name="B nova", family="nova"),
        _model(id="c", name="C claude", family="claude"),
    )
    filtered = ModelFilter(family="claude").apply(models)
    assert [m.id for m in filtered] == ["a", "c"]


def test_filter_apply_returns_tuple() -> None:
    out = ModelFilter().apply((_model(),))
    assert isinstance(out, tuple)
