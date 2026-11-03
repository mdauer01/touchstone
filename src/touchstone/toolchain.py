"""The trusted toolchain: parser and optimizer versions are part of what a
certificate certifies.

Canonical equality is computed by sqlglot's parser and optimizer. Its AST
shapes and rewrite rules change between releases — under 30.24 GROUP BY
ROLLUP moved into `group.expressions`, the ordinal resolver went blind, and
previously tested false certifications reopened while every test still ran.
A dependency range (`>=25`) let any of those versions certify.

So the parser version is pinned, and at runtime a version outside the
qualified set WITHHOLDS certification instead of quietly issuing it under a
trust boundary nobody tested. A new version joins QUALIFIED_SQLGLOT only
after `scripts/qualify_sqlglot.sh` passes the suite, the equivalence
benchmark and the execution oracle under it.
"""

from __future__ import annotations

import os

import sqlglot

# The compatibility matrix. Each entry passed scripts/qualify_sqlglot.sh:
#   30.12.0  pinned install; suite + bench + full oracle (2026-10-24)
#   30.24.0  suite 549/549, bench 0 false certs x4 dialects, full oracle
#            0 disagreements over 91,769 executed pairs (2026-10-24)
QUALIFIED_SQLGLOT: frozenset[str] = frozenset({"30.12.0", "30.24.0"})


def sqlglot_version() -> str:
    return getattr(sqlglot, "__version__", "unknown")


def toolchain_status() -> dict:
    """{"sqlglot": version, "qualified": bool, "qualifying": bool}.

    `qualifying` is true only while the qualification script runs a
    CANDIDATE version (TOUCHSTONE_QUALIFY_SQLGLOT names that exact version):
    the gate must be lifted to test whether the candidate would certify
    correctly, and the scan records that it was a qualification run.
    """
    version = sqlglot_version()
    qualifying = os.environ.get("TOUCHSTONE_QUALIFY_SQLGLOT") == version
    return {"sqlglot": version,
            "qualified": version in QUALIFIED_SQLGLOT or qualifying,
            "qualifying": qualifying}
