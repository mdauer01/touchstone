"""The `touchstone-comparison/1` evidence record (schema.json beside this file).

One record per metric use. Order of evidence is the point:

1. the ORIGINAL statement as it ran, and the authoritative definition it is
   compared with (with its raw snapshot), and the schemas of what it reads;
2. only then touchstone's own reading — extracted differences, refusal
   reasons, suspected items — under `unverified_observations`, labelled as
   unverified and removable (`without_observations`) for the ablation that
   measures whether they help or mislead a classifier.

Validation is a small, dependency-free reading of the schema's own keywords
(type, required, properties, additionalProperties, const, enum, items,
minLength), so a record that validates here validates against schema.json.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

FORMAT = "touchstone-comparison/1"
OBSERVATIONS_NOTE = ("touchstone's own reading of the SQL — extracted differences, refusal "
                     "reasons and suspected items. UNVERIFIED: the interpretation they rest "
                     "on is not established; weigh the statement and the definition first")


@lru_cache(maxsize=1)
def schema() -> dict:
    return json.loads((Path(__file__).parent / "schema.json").read_text(encoding="utf-8"))


def record_id(statement: str, execution_id: str | None, definition_key: str) -> str:
    raw = json.dumps([statement, execution_id, definition_key], sort_keys=True)
    return "cmp_" + hashlib.sha256(raw.encode()).hexdigest()[:16]


def without_observations(record: dict) -> dict:
    """The same record with touchstone's unverified reading removed."""
    return {k: v for k, v in record.items() if k != "unverified_observations"}


def validate(record: Any) -> list[str]:
    """Every way `record` departs from touchstone-comparison/1 (empty = valid)."""
    return _check(record, schema(), "$")


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is(value: Any, name: str) -> bool:
    if name in ("integer", "number"):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        return ok and (name == "number" or float(value).is_integer())
    return isinstance(value, _TYPES[name])


def _check(value: Any, rule: dict, path: str) -> list[str]:
    errors: list[str] = []
    if "const" in rule and value != rule["const"]:
        return [f"{path}: must be {rule['const']!r}"]
    if "enum" in rule and value not in rule["enum"]:
        return [f"{path}: must be one of {rule['enum']}"]
    types = rule.get("type")
    if types is not None:
        names = types if isinstance(types, list) else [types]
        if not any(_is(value, n) for n in names):
            return [f"{path}: expected {' or '.join(names)}, got {type(value).__name__}"]
    if isinstance(value, str) and len(value) < rule.get("minLength", 0):
        errors.append(f"{path}: must not be empty")
    if isinstance(value, dict):
        props = rule.get("properties", {})
        for key in rule.get("required", []):
            if key not in value:
                errors.append(f"{path}: missing {key}")
        extra = rule.get("additionalProperties", True)
        for key, item in value.items():
            if key in props:
                errors += _check(item, props[key], f"{path}.{key}")
            elif extra is False:
                errors.append(f"{path}: unexpected field {key}")
            elif isinstance(extra, dict):
                errors += _check(item, extra, f"{path}.{key}")
    if isinstance(value, list) and isinstance(rule.get("items"), dict):
        for i, item in enumerate(value):
            errors += _check(item, rule["items"], f"{path}[{i}]")
    return errors
