"""The proof grammar is FROZEN (round seventeen).

Positive verdicts are suspended, and they may be re-enabled only with an
explicitly defined grammar, a stated justification for every transformation
and evidence that parsing matches each target engine (docs/proof-policy.md).
Growing the grammar while that evidence does not exist would widen exactly
the surface the reviews kept finding false certifications in, so every
allow-list and rule is pinned here. A change that fails this test needs a
new policy id and the re-enable criteria — not an updated snapshot.

Removing an entry (refusing more) is always allowed: the snapshot is the
upper bound, checked as a subset.
"""

from __future__ import annotations

from touchstone.extraction import proof

FROZEN_POLICY = "touchstone-proof/2"
FROZEN_RULES = {"between", "commutative", "conjuncts", "group_all", "group_ordinal",
                "identifiers", "inline", "not_equality", "order", "output_alias", "parens",
                "star", "table_alias", "unused_cte"}
FROZEN_AGGREGATES = {"Avg", "Count", "Max", "Min", "Sum"}
FROZEN_SCALAR_FUNCTIONS = {
    "Abs", "Case", "Cast", "Ceil", "Coalesce", "Concat", "CurrentDate", "CurrentTimestamp",
    "DateTrunc", "DatetimeTrunc", "Day", "Exp", "Extract", "Floor", "Greatest", "If", "Least",
    "Length", "Ln", "Lower", "Month", "Nullif", "Pow", "Quarter", "Round", "Sign", "Sqrt",
    "Substring", "TimestampTrunc", "Trim", "TryCast", "Upper", "Year"}
FROZEN_EXPRESSIONS = FROZEN_AGGREGATES | FROZEN_SCALAR_FUNCTIONS | {
    "Add", "Alias", "And", "Between", "Boolean", "Column", "DataType", "DataTypeParam",
    "Distinct", "Div", "EQ", "Filter", "GT", "GTE", "ILike", "Identifier", "In", "IntDiv",
    "Interval", "Is", "LT", "LTE", "Like", "Literal", "Mul", "NEQ", "Neg", "Not", "Null",
    "NullSafeEQ", "NullSafeNEQ", "Or", "Ordered", "Paren", "Star", "Sub", "Tuple", "Var",
    "Where"}
FROZEN_STRUCTURE = {"CTE", "From", "Group", "Having", "Order", "Select", "Subquery", "Table",
                    "TableAlias", "With"}
FROZEN_SELECT_ARGS = {"expressions", "from", "from_", "group", "having", "order", "where",
                      "with", "with_"}
FROZEN_TABLE_ARGS = {"alias", "catalog", "db", "this"}
FROZEN_GROUP_ARGS = {"all", "expressions"}

WHY = ("the proof grammar is frozen while positive verdicts are suspended — a new "
       "construct needs a policy-id bump and the re-enable criteria in "
       "docs/proof-policy.md, not a snapshot update")


def _names(classes) -> set[str]:
    return {c.__name__ for c in classes}


def test_the_policy_id_is_unchanged():
    assert proof.POLICY_ID == FROZEN_POLICY, WHY


def test_no_rule_joins_the_policy():
    assert set(proof.RULES) <= FROZEN_RULES, (set(proof.RULES) - FROZEN_RULES, WHY)


def test_no_node_joins_the_grammar():
    for actual, frozen in ((_names(proof._AGGREGATES), FROZEN_AGGREGATES),
                           (_names(proof._SCALAR_FUNCTIONS), FROZEN_SCALAR_FUNCTIONS),
                           (_names(proof._EXPRESSIONS), FROZEN_EXPRESSIONS),
                           (_names(proof._STRUCTURE), FROZEN_STRUCTURE)):
        assert actual <= frozen, (actual - frozen, WHY)


def test_no_argument_joins_the_grammar():
    for actual, frozen in ((proof._SELECT_ARGS, FROZEN_SELECT_ARGS),
                           (proof._TABLE_ARGS, FROZEN_TABLE_ARGS),
                           (proof._GROUP_ARGS, FROZEN_GROUP_ARGS)):
        assert set(actual) <= frozen, (set(actual) - frozen, WHY)


def test_round_seventeen_refusals_stay_out():
    """`%` left the grammar in round seventeen: the parser binds it like `+`."""
    assert "Mod" not in _names(proof._EXPRESSIONS)
