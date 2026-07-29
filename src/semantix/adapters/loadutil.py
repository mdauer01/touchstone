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


def load_yaml(path: str | Path, expect: type = dict) -> Any:
    p = Path(path)
    try:
        data = yaml.safe_load(read_text(p))
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
