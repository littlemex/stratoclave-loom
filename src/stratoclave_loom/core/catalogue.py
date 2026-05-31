"""Shared file-backed model catalogue loader.

Adapters that surface a curated catalogue (claude_code today, future
local-model adapters tomorrow) can load it from a JSON file via this
helper instead of hard-coding it in source. Operators add a new
model id by appending to the JSON file -- no Python edit needed,
no rebuild of the wheel.

Why JSON, not Python literals
-----------------------------
* JSON is editable by humans without an interpreter.
* The packaged file ships in the wheel via ``hatch`` data hooks; an
  external operator-managed override (set via env var or
  ``BackendConfig.extra``) is also valid.
* The schema is explicit (``ModelInfo`` fields), so a malformed file
  is rejected at load time with a clear error.

Resolution order
----------------
The loader looks for the catalogue in this order, falling back to the
next when missing or malformed:

1. ``BackendConfig.extra[<extra_key>]`` -- absolute path
2. ``$ENV_KEY`` (per-adapter env var)
3. The packaged default path passed in by the adapter
4. Empty tuple (``list_models`` returns nothing; the picker still
   renders a free-text input on the SPA side, so the operator can
   type any model id by hand)
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path

from stratoclave_loom.core.types import ModelInfo

logger = logging.getLogger(__name__)


def load_catalogue_file(path: str | os.PathLike[str]) -> tuple[ModelInfo, ...]:
    """Parse a JSON catalogue file into a tuple of :class:`ModelInfo`.

    Returns an empty tuple when the file is missing, unreadable, or
    malformed -- adapter callers degrade to the empty-list path so
    the picker still works (the operator can type a model id by
    hand). Errors are logged so an operator who expected a non-empty
    list can investigate.
    """

    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except OSError:
        logger.exception("could not read catalogue file %s", p)
        return ()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.exception("malformed JSON in catalogue file %s", p)
        return ()
    return _parse_payload(payload)


def _parse_payload(payload: object) -> tuple[ModelInfo, ...]:
    """Translate a parsed JSON payload into ``ModelInfo`` entries.

    Tolerates two shapes:

    * ``{"models": [...]}``  -- the canonical wrapper, lets the file
      carry a ``_comment`` sibling without polluting the list.
    * ``[...]`` -- bare array, useful for tiny ad-hoc catalogues.

    Entries missing ``id`` are dropped; entries missing ``name`` fall
    back to ``id``. Unknown keys are stashed into ``ModelInfo.extra``
    so the SPA picker / future tooling can read them without breaking
    on schema drift.
    """

    if isinstance(payload, dict):
        items = payload.get("models", [])
    elif isinstance(payload, list):
        items = payload
    else:
        return ()
    if not isinstance(items, list):
        return ()
    out: list[ModelInfo] = []
    known = {"id", "name", "family", "provider", "description"}
    for entry in items:
        if not isinstance(entry, Mapping):
            continue
        model_id = entry.get("id")
        if not isinstance(model_id, str) or not model_id:
            continue
        extra: dict[str, object] = {}
        for key, value in entry.items():
            if key in known:
                continue
            extra[str(key)] = value
        out.append(
            ModelInfo(
                id=model_id,
                name=str(entry.get("name") or model_id),
                family=_optional_str(entry.get("family")),
                provider=_optional_str(entry.get("provider")),
                description=_optional_str(entry.get("description")),
                extra=extra,
            )
        )
    return tuple(out)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def resolve_catalogue_path(
    extra: Mapping[str, object] | None,
    *,
    extra_key: str,
    env_key: str,
    default_path: str | os.PathLike[str],
) -> Path:
    """Pick the active catalogue file according to the precedence above.

    The packaged default (``default_path``) is always returned when
    nothing else is set; existence is the caller's concern (the
    loader logs a missing-file warning and degrades to empty).
    """

    if extra is not None:
        explicit = extra.get(extra_key)
        if isinstance(explicit, str) and explicit:
            return Path(explicit)
    env_value = os.environ.get(env_key)
    if env_value:
        return Path(env_value)
    return Path(default_path)


__all__ = [
    "load_catalogue_file",
    "resolve_catalogue_path",
]
