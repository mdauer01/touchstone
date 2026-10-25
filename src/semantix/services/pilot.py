"""Pilot package: everything a monitoring run needs that a query-history
export alone does not carry.

A history export says what ran. It does not say which definitions were
authoritative, what they were at the time each query ran, or which session
defaults a short table name was resolved under. A finding that guesses any
of those accuses someone on evidence nobody supplied, so the package states
them explicitly and the loader refuses a package that leaves one out:

    history: history.jsonl
    window: {from: 2026-10-01T00:00:00Z, to: 2026-10-15T00:00:00Z}
    execution_context:            # optional
      catalog: main
      schema: finance
      asserted_for_all_executions: true
    authority:
      system: uc_metrics
      metric_views:
        - name: main.finance.revenue_mv      # fully qualified
          snapshot: views/revenue_mv.yaml    # the definition text
          sha256: <hex>                      # of the snapshot file
          applies: {from: 2026-09-01T00:00:00Z, to: null}
    compare_against: as_of_execution        # or: current
    snapshots_taken_at: 2026-10-16T09:00:00Z   # optional; shown in `current` mode
    catalog: catalog.yml                     # optional column catalog
    asset_map: {"dashboard:01ef…": net_revenue}   # optional identity hints

Unknown keys are refused, as everywhere else: a key this loader does not
read is a statement about the run that would silently not hold.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from semantix.adapters.base import DeclaredSpec, spec_content_text
from semantix.adapters.loadutil import load_yaml
from semantix.adapters.uc_metrics import load_uc_metrics
from semantix.store import Store, normalize_ts

_TOP_KEYS = frozenset({"history", "window", "execution_context", "authority",
                       "compare_against", "snapshots_taken_at", "catalog",
                       "asset_map"})
_CONTEXT_KEYS = frozenset({"catalog", "schema", "asserted_for_all_executions"})
_AUTHORITY_KEYS = frozenset({"system", "metric_views"})
_VIEW_KEYS = frozenset({"name", "snapshot", "sha256", "applies"})
_WINDOW_KEYS = frozenset({"from", "to"})
COMPARE_MODES = ("as_of_execution", "current")


@dataclass
class PilotView:
    name: str                      # fully qualified, lower-cased
    snapshot: Path
    sha256: str
    applies_from: str | None
    applies_to: str | None
    specs: list[DeclaredSpec] = field(default_factory=list)

    def applies_at(self, ts: str) -> bool:
        return ((self.applies_from is None or self.applies_from <= ts)
                and (self.applies_to is None or ts < self.applies_to))

    @property
    def measure_names(self) -> set[str]:
        return {s.name.lower() for s in self.specs}


@dataclass
class Pilot:
    root: Path
    history: Path
    compare_against: str
    views: list[PilotView]
    window_from: str | None = None
    window_to: str | None = None
    context_catalog: str | None = None
    context_schema: str | None = None
    context_asserted: bool = False
    snapshots_taken_at: str | None = None
    catalog: Path | None = None
    asset_map: dict[str, str] = field(default_factory=dict)
    system: str = "uc_metrics"

    def view(self, relation: str) -> PilotView | None:
        want = relation.lower()
        return next((v for v in self.views if v.name == want), None)

    def comparison_label(self) -> str:
        if self.compare_against == "current":
            when = self.snapshots_taken_at or "an unstated time"
            return (f"compared against the definition snapshot taken {when}; "
                    f"not verified as of execution time")
        return "compared against the definition that applied at execution time"


def load_pilot(directory: str | Path, dialect: str) -> Pilot:
    root = Path(directory)
    manifest = root / "pilot.yml"
    if not manifest.is_file():
        raise ValueError(f"{root}: no pilot.yml — a pilot package is a directory "
                         f"with a pilot.yml manifest (see docs/pilot.md)")
    data = load_yaml(manifest)
    _refuse_unknown(data, _TOP_KEYS, "pilot.yml")

    history = _required_path(root, data.get("history"), "history")
    compare = data.get("compare_against")
    if compare not in COMPARE_MODES:
        raise ValueError(f"pilot.yml: compare_against must be one of "
                         f"{', '.join(COMPARE_MODES)} — say explicitly whether "
                         f"findings are judged against the definition at "
                         f"execution time or against today's")

    window = data.get("window") or {}
    if not isinstance(window, dict):
        raise ValueError("pilot.yml: window must be a mapping with from/to")
    _refuse_unknown(window, _WINDOW_KEYS, "pilot.yml window")

    ctx = data.get("execution_context") or {}
    if not isinstance(ctx, dict):
        raise ValueError("pilot.yml: execution_context must be a mapping")
    _refuse_unknown(ctx, _CONTEXT_KEYS, "pilot.yml execution_context")
    asserted = ctx.get("asserted_for_all_executions", False)
    if not isinstance(asserted, bool):
        raise ValueError("pilot.yml: execution_context.asserted_for_all_executions "
                         "must be true or false")
    if asserted and not (ctx.get("catalog") or ctx.get("schema")):
        raise ValueError("pilot.yml: execution_context asserts a context for all "
                         "executions but names no catalog or schema")

    authority = data.get("authority")
    if not isinstance(authority, dict):
        raise ValueError("pilot.yml: authority is required — the monitor judges "
                         "executions against definitions someone declared "
                         "authoritative, never against whatever it finds")
    _refuse_unknown(authority, _AUTHORITY_KEYS, "pilot.yml authority")
    system = authority.get("system")
    if system != "uc_metrics":
        raise ValueError(f"pilot.yml: authority.system {system!r} is not supported "
                         f"— this release reads Unity Catalog metric views "
                         f"(uc_metrics) only")
    entries = authority.get("metric_views")
    if not isinstance(entries, list) or not entries:
        raise ValueError("pilot.yml: authority.metric_views must list at least "
                         "one approved metric view")

    views: list[PilotView] = []
    for i, entry in enumerate(entries):
        where = f"pilot.yml authority.metric_views[{i}]"
        if not isinstance(entry, dict):
            raise ValueError(f"{where}: not a mapping")
        _refuse_unknown(entry, _VIEW_KEYS, where)
        name = str(entry.get("name") or "").replace("`", "").strip().lower()
        if len([p for p in name.split(".") if p]) != 3:
            raise ValueError(f"{where}: metric view name {entry.get('name')!r} is "
                             f"not fully qualified (catalog.schema.view) — a "
                             f"short name identifies a different view in every "
                             f"session")
        snapshot = _required_path(root, entry.get("snapshot"), f"{where}.snapshot")
        declared_sha = str(entry.get("sha256") or "").strip().lower()
        actual_sha = hashlib.sha256(snapshot.read_bytes()).hexdigest()
        if not declared_sha:
            raise ValueError(f"{where}: sha256 of the snapshot is required "
                             f"(it is {actual_sha})")
        if declared_sha != actual_sha:
            raise ValueError(f"{where}: snapshot {snapshot.name} does not match its "
                             f"declared sha256 (declared {declared_sha[:12]}…, file "
                             f"is {actual_sha[:12]}…) — the definition changed "
                             f"after it was recorded, or the wrong file is here")
        applies = entry.get("applies")
        if applies is not None and not isinstance(applies, dict):
            raise ValueError(f"{where}: applies must be a mapping with from/to")
        applies = applies or {}
        _refuse_unknown(applies, _WINDOW_KEYS, f"{where}.applies")
        if compare == "as_of_execution" and applies.get("from") is None:
            raise ValueError(f"{where}: compare_against is as_of_execution but no "
                             f"applies.from is given — without evidence of when "
                             f"this definition applied, an execution cannot be "
                             f"judged against it (or set compare_against: current)")
        specs, _ = load_uc_metrics(snapshot, dialect)
        views.append(PilotView(
            name=name, snapshot=snapshot, sha256=actual_sha,
            applies_from=_ts(applies.get("from"), f"{where}.applies.from"),
            applies_to=_ts(applies.get("to"), f"{where}.applies.to"),
            specs=specs))
    names = [v.name for v in views]
    if len(set(names)) != len(names):
        raise ValueError("pilot.yml: a metric view is listed twice")

    asset_map = data.get("asset_map") or {}
    if not isinstance(asset_map, dict):
        raise ValueError("pilot.yml: asset_map must map asset ids to metric names")
    catalog = data.get("catalog")
    return Pilot(
        root=root, history=history, compare_against=compare, views=views,
        window_from=_ts(window.get("from"), "pilot.yml window.from"),
        window_to=_ts(window.get("to"), "pilot.yml window.to"),
        context_catalog=_str(ctx.get("catalog")), context_schema=_str(ctx.get("schema")),
        context_asserted=asserted,
        snapshots_taken_at=_ts(data.get("snapshots_taken_at"), "pilot.yml snapshots_taken_at"),
        catalog=_required_path(root, catalog, "catalog") if catalog else None,
        asset_map={str(k): str(v) for k, v in asset_map.items()},
        system=system)


def verify_against_scan(pilot: Pilot, store: Store, scan_id: str) -> None:
    """The authority the monitor judges against must be what the scan read:
    every snapshot measure recorded by the latest scan with byte-identical
    declared content. Otherwise the scan's verdicts (contested or not) are
    about a different definition than the one findings will cite."""
    recorded: dict[str, set[str]] = {}
    for row in store.scan_snapshot(scan_id):
        if row["kind"] == "declared" and row["system"] == pilot.system:
            recorded.setdefault(row["object_id"], set()).add(row["raw_sha"])
    missing = []
    for view in pilot.views:
        for spec in view.specs:
            sha = hashlib.sha256(spec_content_text(spec).encode()).hexdigest()
            if sha not in recorded.get(spec.name, set()):
                missing.append(f"{view.name}/{spec.name}")
    if missing:
        raise ValueError(
            f"the latest scan did not read the pilot's definition of "
            f"{', '.join(missing)} — scan the snapshots first, with the same "
            f"--dialect the monitor uses (`semantix scan --uc-metrics "
            f"{pilot.root}/<snapshots dir> …`), so the authority findings cite is "
            f"the one the scan verified")


def _refuse_unknown(mapping: dict, allowed: frozenset, where: str) -> None:
    unknown = sorted(str(k) for k in set(mapping) - allowed)
    if unknown:
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)} — refusing "
                         f"to ignore part of the package")


def _required_path(root: Path, value: object, what: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"pilot.yml: {what} is required")
    p = (root / value).resolve()
    if not p.is_file():
        raise ValueError(f"pilot.yml: {what} {value!r} not found under {root}")
    return p


def _str(value: object) -> str | None:
    return str(value).strip() or None if value is not None else None


def _ts(value: object, what: str) -> str | None:
    if value is None:
        return None
    # YAML reads an unquoted ISO instant as a datetime.
    if isinstance(value, datetime):
        value = value.isoformat()
    elif isinstance(value, date):
        value = f"{value.isoformat()}T00:00:00Z"
    try:
        return normalize_ts(str(value))
    except ValueError as e:
        raise ValueError(f"{what}: {e}") from None
