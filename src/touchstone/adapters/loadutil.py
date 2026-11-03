"""Shared, honest file intake for every adapter.

`dbt.py` and `sql_dir.py` carefully guard encoding and OSError for COMPILED
SQL, but every manifest / YAML / JSON read was unguarded — so one malformed
file in an estate took the whole scan down with a raw traceback instead of
being reported. Worse, a truncated manifest raised `Expecting ',' delimiter:
line 1 column 42` without naming the file, which is unactionable when a scan
reads several.

Every loader routes its reads through here so the failure names the file, says
what was wrong with it, and arrives as a ValueError the CLI can present.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def read_text(path: str | Path) -> str:
    """File contents, or a ValueError naming the file and the reason."""
    p = Path(path)
    try:
        # utf-8-sig: a BOM is invisible and breaks every parser downstream.
        return p.read_text(encoding="utf-8-sig", errors="strict")
    except FileNotFoundError as e:
        raise ValueError(f"{p}: file not found") from e
    except UnicodeDecodeError as e:
        raise ValueError(
            f"{p}: not valid UTF-8 ({e.reason} at byte {e.start}) — re-save the "
            f"file as UTF-8"
        ) from e
    except OSError as e:
        raise ValueError(f"{p}: could not be read ({e.strerror})") from e


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a key written twice in one mapping. YAML keeps
    the LAST value and drops the first without a word — an approval, a
    dimension or a filter written twice was silently half-read (round 18)."""

    def flatten_mapping(self, node):
        # PyYAML flattens a merged mapping IN PLACE — sometimes while building
        # another mapping that merges it — so record what was WRITTEN first:
        # an explicit key overriding a merged one is not written twice.
        if not hasattr(node, "_touchstone_written"):
            node._touchstone_written = list(node.value)
        super().flatten_mapping(node)

    def construct_mapping(self, node, deep=False):
        if isinstance(node, yaml.MappingNode):
            seen: set = set()
            for key_node, _ in getattr(node, "_touchstone_written", node.value):
                if key_node.tag == "tag:yaml.org,2002:merge":
                    continue    # `<<:` merges; explicit keys may override merged ones
                key = self.construct_object(key_node, deep=deep)
                try:
                    duplicate = key in seen
                except TypeError:
                    continue    # unhashable; the base constructor reports it
                if duplicate:
                    raise yaml.constructor.ConstructorError(
                        "while constructing a mapping", node.start_mark,
                        f"the key {key!r} appears twice — YAML would keep only the "
                        f"last value", key_node.start_mark)
                seen.add(key)
        return super().construct_mapping(node, deep=deep)


class _TextTimestampLoader(_StrictLoader):
    """Strict loader that leaves ISO instants as the text written: YAML's own
    timestamp type truncates to microseconds and drops comma fractions."""


_TextTimestampLoader.yaml_implicit_resolvers = {
    key: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in _StrictLoader.yaml_implicit_resolvers.items()
}


def parse_yaml(text: str, where: str, *, timestamps_as_text: bool = False) -> Any:
    """YAML text (an embedded document, such as a snapshot inside a dataset
    file) read with the same strict loader as load_yaml."""
    try:
        return yaml.load(text,  # noqa: S506 — both are SafeLoaders
                         Loader=_TextTimestampLoader if timestamps_as_text else _StrictLoader)
    except yaml.YAMLError as e:
        raise ValueError(f"{where}: is not valid YAML — {e}") from e


def load_yaml(path: str | Path, expect: type = dict, *,
              timestamps_as_text: bool = False) -> Any:
    p = Path(path)
    try:
        data = yaml.load(read_text(p),  # noqa: S506 — both are SafeLoaders
                         Loader=_TextTimestampLoader if timestamps_as_text else _StrictLoader)
    except yaml.YAMLError as e:
        raise ValueError(f"{p}: is not valid YAML — {e}") from e
    if data is None:
        return expect()
    if not isinstance(data, expect):
        hint = (" — if this is a bare list of entries, wrap it under the "
                "expected top-level key (e.g. `metrics:`)"
                if isinstance(data, list) and expect is dict else "")
        raise ValueError(
            f"{p}: expected a YAML {expect.__name__} at the top level, found "
            f"{type(data).__name__}{hint}")
    return data


def load_json(path: str | Path, expect: type = dict) -> Any:
    p = Path(path)
    try:
        data = json.loads(read_text(p))
    except json.JSONDecodeError as e:
        raise ValueError(f"{p}: is not valid JSON — {e}") from e
    if not isinstance(data, expect):
        raise ValueError(
            f"{p}: expected a JSON {expect.__name__} at the top level, found "
            f"{type(data).__name__}")
    return data
