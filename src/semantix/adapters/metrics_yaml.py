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

import yaml

from semantix.adapters.base import DeclaredSpec


def load_metrics(path: str | Path) -> list[DeclaredSpec]:
    p = Path(path)
    data = yaml.safe_load(p.read_text()) or {}
    specs = []
    for entry in data.get("metrics") or []:
        if not entry.get("name"):
            continue
        specs.append(
            DeclaredSpec(
                system="metrics_yaml",
                name=str(entry["name"]),
                description=entry.get("description"),
                owner=entry.get("owner"),
                model=entry.get("model"),
                expr=entry.get("expr"),
                filters=[str(f) for f in entry.get("filters") or []],
                group_by=[str(g) for g in entry.get("group_by") or []],
                path=str(p),
            )
        )
    return specs
