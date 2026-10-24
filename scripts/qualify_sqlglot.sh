#!/usr/bin/env bash
# Qualify a candidate sqlglot version for semantix.
#
# The parser and optimizer ARE part of the trusted implementation: equality is
# computed in sqlglot's canonical form, and a release that moves an AST shape
# can reopen false certifications while every test still runs. A version may
# join QUALIFIED_SQLGLOT (src/semantix/toolchain.py) only if all three pass:
#   1. the test suite
#   2. the equivalence benchmark (every dialect in the matrix)
#   3. the execution oracle, full cross product
set -euo pipefail
VERSION="${1:?usage: scripts/qualify_sqlglot.sh <sqlglot-version>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install -q "sqlglot==$VERSION" pyyaml "cryptography>=42" "duckdb>=1" "pytest>=8"
# Lifts the runtime gate for THIS candidate only, so the run tests whether it
# would certify correctly rather than measuring the gate.
export PYTHONPATH="$ROOT/src" SEMANTIX_QUALIFY_SQLGLOT="$VERSION"
echo "== suite (sqlglot $VERSION)";  "$WORK/venv/bin/python" -m pytest -q -p no:cacheprovider "$ROOT/tests"
echo "== benchmark";                 "$WORK/venv/bin/python" -m semantix.cli bench
echo "== execution oracle (full)";   "$WORK/venv/bin/python" -m semantix.cli oracle --full
echo
echo "sqlglot $VERSION passed qualification. Add it to QUALIFIED_SQLGLOT in"
echo "src/semantix/toolchain.py, and record the run in the commit message."
