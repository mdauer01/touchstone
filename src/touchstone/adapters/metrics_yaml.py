"""Declared-semantics adapter: a MetricFlow-flavored metrics YAML.

Format:

    metrics:
      - name: net_revenue
        description: ...
        owner: finance
        model: analytics.fct_orders
        expr: "sum(amount - refund_amount)"
        filters: ["status = 'completed'"]
        group_by: ["date_trunc('month', order_date)"]
"""

from __future__ import annotations

from pathlib import Path

from touchstone.adapters.loadutil import load_yaml

from touchstone.adapters.base import DeclaredSpec


def _sql_terms(value, label: str) -> tuple[list[str], list[str]]:
    """(terms, untranslatable) from a YAML field that should be a list of SQL
    fragments.

    A bare string is the common authoring slip (`filters: "status = 'x'"`),
    and iterating it yields ONE BOGUS PREDICATE PER CHARACTER — so it is read
    as the single fragment it plainly is. Anything that is not a string, such
    as a nested mapping, is not a SQL fragment at all and is reported rather
    than str()'d into one.
    """
    if value is None:
        return [], []
    if isinstance(value, str):
        return ([value] if value.strip() else []), []
    if not isinstance(value, (list, tuple)):
        return [], [f"{label}: expected a list of SQL fragments, got {type(value).__name__}"]
    terms, bad = [], []
    for item in value:
        if isinstance(item, str) and item.strip():
            terms.append(item)
        else:
            bad.append(f"{label}: {item!r} is not a SQL fragment")
    return terms, bad


def load_metrics(path: str | Path) -> list[DeclaredSpec]:
    p = Path(path)
    data = load_yaml(p)
    entries = data.get("metrics") or []
    # A wrong-shape `metrics:` — a string, a mapping — was silently ignored:
    # the scan ran green with `claims 0`, dropping every declaration in the
    # ONE file the customer authors by hand. Silent ontology loss is the
    # cardinal sin; a shape error must name the file and the shape.
    if not isinstance(entries, list):
        raise ValueError(
            f"{p}: `metrics` must be a list of metric entries, found "
            f"{type(entries).__name__} — no declarations were read")

    specs = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(
                f"{p}: metrics[{index}] must be a mapping, found "
                f"{type(entry).__name__}: {entry!r}")
        if not entry.get("name"):
            raise ValueError(
                f"{p}: metrics[{index}] has no `name` — a nameless declaration "
                f"cannot be reconciled and would be silently dropped")
        filters, bad_filters = _sql_terms(entry.get("filters"), "filters")
        group_by, bad_group = _sql_terms(entry.get("group_by"), "group_by")

        # Scalar fields need the same guard as the list ones. A mapping or a
        # list where a string belongs reached the store as a bind parameter
        # and killed the scan mid-write with a SQLite type error.
        scalars: dict[str, str | None] = {}
        bad_scalars: list[str] = []
        for field_name in ("model", "expr", "description", "owner"):
            value = entry.get(field_name)
            if value is None or isinstance(value, str):
                scalars[field_name] = value
            elif isinstance(value, (int, float, bool)):
                scalars[field_name] = str(value)
            else:
                scalars[field_name] = None
                bad_scalars.append(
                    f"{field_name}: expected text, got {type(value).__name__}"
                )
        specs.append(
            DeclaredSpec(
                system="metrics_yaml",
                name=str(entry["name"]),
                description=scalars["description"],
                owner=scalars["owner"],
                model=scalars["model"],
                expr=scalars["expr"],
                filters=filters,
                group_by=group_by,
                untranslatable=bad_filters + bad_group + bad_scalars,
                path=str(p),
            )
        )
    return specs
