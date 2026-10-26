"""Session state in a query history: reconstructed only from evidence.

What a short table name reads depends on the session that ran it — its USE,
SET, temporary views, and anything else it ran before. A query history rarely
carries all of that. The rule here: context is RECONSTRUCTED only when the
history supplies it with certain order (USE statements earlier in the same
execution); anything that may have changed a name's meaning and whose order
or session the history does not establish makes that name's identity
UNCERTAIN. Context is never guessed.

Events and what they can change:

* USE / SET / an unparseable or unrecognized statement — every SHORT name
  (fewer parts than the estate's names, as written). Fully qualified names
  do not depend on session defaults.
* CREATE / DROP / ALTER / CACHE of a relation — that relation's name: a temp
  view `orders` shadows the short reference `orders`.
* INSERT / UPDATE / DELETE / MERGE / SHOW / DESCRIBE / EXPLAIN / COMMENT /
  REFRESH / transaction control — nothing a name resolves to.

An event may precede a statement CERTAINLY (earlier in the same execution) or
POSSIBLY (another execution of the same or an unknown session, not provably
later). Either kind of affecting event makes the statement's relation
identity depend on the session, unless every affecting event is a certain,
fully parsed USE — then the context is reconstructed.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

from semantix.extraction.sqlutil import table_fqname

# Statement classes that change nothing a name resolves to.
_NEUTRAL = tuple(getattr(exp, n) for n in (
    "Insert", "Update", "Delete", "Merge", "Describe", "Comment", "Refresh",
    "Transaction", "Commit", "Rollback", "Analyze", "Pragma", "Copy")
    if hasattr(exp, n))
_NEUTRAL_COMMANDS = ("SHOW", "DESCRIBE", "DESC", "EXPLAIN", "LIST", "COMMENT", "REFRESH",
                     "ANALYZE", "BEGIN", "COMMIT", "ROLLBACK", "START")
# USE targets that select compute or privileges, not a namespace.
_NEUTRAL_USE_KINDS = {"WAREHOUSE", "ROLE", "SECONDARY", "SECONDARY ROLES"}


@dataclass(frozen=True)
class SessionEvent:
    row: int
    index: int
    session: str | None
    ts: str | None
    what: str                        # as a human reads it: "USE main.finance"
    names: frozenset[str] | None     # None: every short name
    # A fully parsed USE: (catalog or None, schema or None, schema_known).
    use: tuple[str | None, str | None, bool] | None = None


def classify(stmt: exp.Expression | None, row: int, index: int, session: str | None,
             ts: str | None) -> SessionEvent | None:
    """The session event a non-query statement is, or None when it changes
    nothing a name resolves to. `stmt` None is an unparseable statement."""
    def event(what, names=None, use=None):
        return SessionEvent(row, index, session, ts, what, names, use)

    if stmt is None:
        return event("an unparseable statement")
    if isinstance(stmt, exp.Query):
        return None
    if isinstance(stmt, _NEUTRAL):
        return None
    if isinstance(stmt, exp.Command):
        head = str(stmt.this or "").strip().upper()
        if head.split(" ")[0] in _NEUTRAL_COMMANDS:
            return None
        return event(f"{head or 'a'} command")
    if isinstance(stmt, exp.Use):
        return _use_event(stmt, event)
    if isinstance(stmt, exp.Set):
        return event("SET")
    created = (exp.Create, exp.Drop, exp.Cache) + tuple(
        getattr(exp, n) for n in ("Alter", "AlterTable", "Uncache") if hasattr(exp, n))
    if isinstance(stmt, created):
        target = stmt.this
        if isinstance(target, exp.Schema):
            target = target.this
        if isinstance(target, exp.Table) and target.name:
            names = {table_fqname(target).lower()}
            for rename in stmt.find_all(exp.RenameTable) if hasattr(exp, "RenameTable") else ():
                if isinstance(rename.this, exp.Table):
                    names.add(table_fqname(rename.this).lower())
            kind = str(stmt.args.get("kind") or type(stmt).__name__).upper()
            return event(f"{type(stmt).__name__.upper()} {kind} {table_fqname(target)}",
                         frozenset(names))
        return event(f"{type(stmt).__name__.upper()} statement")
    return event(f"{type(stmt).__name__.upper()} statement")


def _use_event(stmt: exp.Use, event) -> SessionEvent | None:
    kind = str((stmt.args.get("kind") or exp.Var(this="")).name).upper()
    if kind in _NEUTRAL_USE_KINDS:
        return None
    target = stmt.this
    parts = None
    if isinstance(target, (exp.Table, exp.Column, exp.Identifier, exp.Dot)):
        text = target.sql()
        if "(" not in text and ":" not in text and "?" not in text:
            parts = [p.strip('`"') for p in text.split(".")]
    what = f"USE {kind + ' ' if kind else ''}{target.sql() if target is not None else ''}".strip()
    if not parts or len(parts) > 2:
        return event(what)
    if kind == "CATALOG":
        if len(parts) != 1:
            return event(what)
        # The schema after USE CATALOG is whatever that catalog's default is:
        # not evidenced, so not assumed.
        return event(what, use=(parts[0], None, False))
    if kind in ("", "SCHEMA", "DATABASE"):
        if len(parts) == 2:
            return event(what, use=(parts[0], parts[1], True))
        if kind == "DATABASE":
            # Snowflake's DATABASE is the catalog level; elsewhere a schema.
            return event(what)
        return event(what, use=(None, parts[0], True))
    return event(what)


def precedes(e: SessionEvent, row: int, index: int, session: str | None,
             ts: str | None, log: bool) -> str | None:
    """"certain", "possible", or None (the event cannot affect the statement).
    `log`: the history is a bare SQL log — order known, sessions not."""
    if e.row == row:
        if e.index >= index:
            return None
        return "possible" if log else "certain"
    if e.session and session and e.session != session:
        return None
    if e.ts is not None and ts is not None and e.ts > ts:
        return None
    return "possible"


def affects(e: SessionEvent, written: list[str], depth: int) -> bool:
    """Whether the event can change what any of the statement's relation
    references (as written, lowercased) resolve to."""
    def short(name: str) -> bool:
        return len(name.split(".")) < depth
    if e.names is None:
        return any(short(r) for r in written)
    for r in written:
        for n in e.names:
            if r == n:
                return True
            if r.split(".")[-1] == n.split(".")[-1] and (short(r) or short(n)):
                return True
    return False


def written_relations(stmt: exp.Expression) -> list[str]:
    ctes = {c.alias_or_name.lower() for c in stmt.find_all(exp.CTE)}
    out = []
    for t in stmt.find_all(exp.Table):
        if not t.name:
            continue
        if not t.args.get("db") and t.name.lower() in ctes:
            continue
        out.append(table_fqname(t).lower())
    return out


def resolve(events: list[SessionEvent], row: int, index: int, session: str | None,
            ts: str | None, log: bool, written: list[str], depth: int,
            context: tuple[str | None, str | None]
            ) -> tuple[tuple[str | None, str | None], str | None, bool]:
    """(context to qualify the statement with, session gap or None,
    reconstructed?) for one query statement."""
    affecting = []
    for e in events:
        how = precedes(e, row, index, session, ts, log)
        if how and affects(e, written, depth):
            affecting.append((e, how))
    if not affecting:
        return context, None, False
    if all(how == "certain" and e.use is not None for e, how in affecting):
        catalog, schema = context
        for e, _ in sorted(affecting, key=lambda t: t[0].index):
            use_catalog, use_schema, schema_known = e.use
            if use_catalog is not None:
                catalog = use_catalog
            schema = use_schema if schema_known else None
        return (catalog, schema), None, True
    e, how = sorted(affecting, key=lambda t: (t[1] != "certain", t[0].row, t[0].index))[0]
    where = ("earlier in this execution" if e.row == row and not log else
             "earlier in this log" if e.row == row else
             "in another execution of this session" if e.session and session else
             "in another execution whose session is not recorded")
    certainty = "precedes" if how == "certain" else "may precede"
    names = ", ".join(sorted(set(written))) or "its names"
    return context, (f"relation identity depends on the session: {e.what} ({where}) "
                     f"{certainty} this statement and can change what {names} "
                     f"resolve(s) to"), False
