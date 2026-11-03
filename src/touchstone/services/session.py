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

import re
from dataclasses import dataclass

from sqlglot import exp

from touchstone.extraction.sqlutil import table_fqname

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
    # The statement's own text as it ran (evidence records), when known.
    text: str | None = None


def classify(stmt: exp.Expression | None, row: int, index: int, session: str | None,
             ts: str | None, text: str | None = None) -> SessionEvent | None:
    """The session event a non-query statement is, or None when it changes
    nothing a name resolves to. `stmt` None is an unparseable statement;
    `text` is its text, when known."""
    text = stmt.meta.get("source_text") if stmt is not None else text

    def event(what, names=None, use=None):
        return SessionEvent(row, index, session, ts, what, names, use, text)

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
            # The NEW name a rename makes readable is touched too. sqlglot
            # 30.x parses it as exp.AlterRename (older releases RenameTable);
            # looking only for RenameTable made this dead code (round 17).
            renames = tuple(getattr(exp, n) for n in ("AlterRename", "RenameTable")
                            if hasattr(exp, n))
            for rename in stmt.find_all(*renames) if renames else ():
                new_name = rename.this
                if isinstance(new_name, exp.Table) and new_name.name:
                    names.add(table_fqname(new_name).lower())
                    if not new_name.args.get("db"):
                        # An unqualified new name lands beside the old one.
                        prefix = table_fqname(target).rsplit(".", 1)
                        if len(prefix) == 2:
                            names.add(f"{prefix[0]}.{new_name.name}".lower())
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


# ------------------------------------------------------------ export text

# SET keys whose values describe how SQL is EVALUATED (ANSI mode, time zone,
# shuffle width): exported. Every other SET value — a storage-account key, an
# S3 secret, a JDBC password set as a conf — is not.
_SAFE_SET = re.compile(r"^(spark\.sql\.|spark\.databricks\.sql\.|ansi_mode$|timezone$|"
                       r"time zone$|statement_timeout$)", re.IGNORECASE)
_SECRETISH = re.compile(r"(key|secret|passw|pwd|token|credential|sas|auth|private|cert|"
                        r"oauth|bearer)", re.IGNORECASE)
_SET_RE = re.compile(r"^\s*SET\s+(?P<key>[^=]+?)\s*=\s*(?P<value>.*?)\s*;?\s*$",
                     re.IGNORECASE | re.DOTALL)
# Clauses whose string values configure a source or a connection.
_OPTION_WORDS = frozenset({"OPTIONS", "TBLPROPERTIES", "PROPERTIES", "CREDENTIALS",
                           "CREDENTIAL", "SERDEPROPERTIES", "DBPROPERTIES"})
_SECRET_WORDS = frozenset({"IDENTIFIED", "PASSWORD"})
NOT_EXPORTED = "'[not exported]'"


def exportable(text: str, dialect: str) -> str:
    """A session statement's text as it may leave touchstone — into an evidence
    export and a classifier's prompt. Statements carry credentials: a SET of
    a storage key, a JDBC password in OPTIONS. Kept: everything that decides
    what SQL reads and computes; removed: the value of a SET whose key is not
    a known evaluation setting, and every string inside OPTIONS / properties
    clauses or after IDENTIFIED BY / PASSWORD. Text that cannot be tokenized
    is not exported."""
    import sqlglot
    from sqlglot.tokens import TokenType

    m = _SET_RE.match(text)
    if m:
        key = m.group("key").strip()
        if _SAFE_SET.match(key) and not _SECRETISH.search(key):
            return text.strip()
        return f"SET {key} = [value not exported]"
    try:
        tokens = sqlglot.tokenize(text, read=dialect)
    except Exception:  # noqa: BLE001 — unknown text never leaves verbatim
        head = text.strip().split(None, 1)[0] if text.strip() else ""
        return f"{head} … [text not exported: it could not be tokenized]".strip()
    spans = []
    depth = 0          # paren depth inside an options clause; 0 = outside
    after_secret = False
    for i, tok in enumerate(tokens):
        word = tok.text.upper()
        if depth == -1:
            depth = 1
            continue
        if depth:
            if tok.token_type == TokenType.L_PAREN:
                depth += 1
            elif tok.token_type == TokenType.R_PAREN:
                depth -= 1
            elif tok.token_type == TokenType.STRING:
                spans.append(tok)
            continue
        if tok.token_type == TokenType.STRING and after_secret:
            spans.append(tok)
        after_secret = word in _SECRET_WORDS or (after_secret and word in ("BY", "="))
        if word in _OPTION_WORDS and i + 1 < len(tokens) \
                and tokens[i + 1].token_type == TokenType.L_PAREN:
            depth = -1                         # the next token opens the clause
    if not spans:
        return text.strip()
    out, last = [], 0
    for tok in spans:
        start, end = tok.start, tok.end + 1      # the span includes the quotes
        out.append(text[last:start])
        out.append(NOT_EXPORTED)
        last = end
    out.append(text[last:])
    return "".join(out).strip()
