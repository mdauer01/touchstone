"""Round twelve: the supported-SQL boundary is settled before ANY violation.

A transformed metric-view projection (`MEASURE(net_revenue) * 2 AS …`)
returned its violation before the statement's shared boundary — filters,
groupings, projected dimensions — was checked, so `WHERE channel = country`
(a predicate the rules do not support) or a filter on a declared but
unsupported dimension became a headline violation; the bare projection
under the same filter was correctly unverifiable. The raw-table path had
the same order: measure and required-filter violations were returned
before an unsupported condition, LIMIT or HAVING was considered.

Now both paths settle the boundary first. Outside it, the use is
unverifiable and what would have been a violation travels as `suspected`
context, never as a verdict and never in the headline.

Under the conclusions policy (touchstone.conclusions) a violation the rules do
find inside the boundary is reported as a suspected difference; these tests
pin the engine's judgment through tests.policy and the reported kind beside it.
"""

from __future__ import annotations

import pytest

from .policy import engine_codes, engine_funnel, engine_kind
from .test_monitor_conformance import GOVERNED, VIEW, _rows, codes, run
from .test_round_ten_fixes import headline_consistent

V = "main.finance.revenue_mv"
VIEW_WITH = (VIEW.format(extra="  - name: size\n    expr: CASE WHEN amount > 100 THEN 'big' "
                               "ELSE 'small' END\n")
             + "  - name: gross_revenue\n    expr: SUM(amount)\n")

# Statement tails outside the supported boundary, and the reason each gives.
UNSUPPORTED = [
    ("WHERE channel = country", "predicate_unsupported"),          # dimension = dimension
    ("WHERE size = 'big'", "dimension_unsupported"),               # declared, unsupported
    ("GROUP BY size", "dimension_unsupported"),                    # unsupported grouping
    ("WHERE country = 'US' AND channel = country", "predicate_unsupported"),
]
PROJECTIONS = [
    ("MEASURE(net_revenue) * 2 AS net_revenue", "net_revenue"),     # original name
    ("MEASURE(net_revenue) * 2 AS gross_revenue", "gross_revenue"),  # another metric's name
    ("MEASURE(net_revenue) * 2", "net_revenue"),                    # unaliased
    ("AGG(net_revenue) * 2 AS net_revenue", "net_revenue"),
    ("AGG(net_revenue) * 2 AS gross_revenue", "gross_revenue"),
    ("ROUND(AGG(net_revenue), 2) AS net_revenue", "net_revenue"),
    ("MEASURE(net_revenue) AS gross_revenue", "gross_revenue"),     # bare, renamed
]


def _select(projection: str, tail: str) -> str:
    grouped = tail.startswith("GROUP BY")
    return f"SELECT {'size, ' if grouped else ''}{projection} FROM {V} {tail}"


@pytest.mark.parametrize("tail, code", UNSUPPORTED)
@pytest.mark.parametrize("projection, concept", PROJECTIONS)
def test_transformed_or_renamed_projection_outside_the_boundary_is_unverifiable(
        tmp_path, projection, concept, tail, code):
    result = run(tmp_path, _rows(_select(projection, tail)), view_text=VIEW_WITH)
    (f,) = result["findings"]
    # The rules themselves leave it unverifiable (not an experimental match,
    # which the policy would also report as unverifiable).
    assert (engine_kind(f), f["concept"]) == ("unverifiable", concept)
    assert f["kind"] == "unverifiable"
    assert code in engine_codes(f) and codes(f) == engine_codes(f)
    assert engine_funnel(result["funnel"])["violations"] == 0
    # What would have been the violation is kept, as context only.
    assert f["suspected"], f
    headline_consistent(result)


@pytest.mark.parametrize("projection, kind, code", [
    ("MEASURE(net_revenue) * 2 AS net_revenue", "definition_violation",
     "transformed_governed_measure"),
    ("AGG(net_revenue) * 2 AS net_revenue", "definition_violation",
     "transformed_governed_measure"),
    ("MEASURE(net_revenue) AS gross_revenue", "definition_violation",
     "presented_under_other_name"),
    # Under ANOTHER metric's name a derived expression may compute it exactly
    # (round fifteen) — not decided, never a violation.
    ("MEASURE(net_revenue) * 2 AS gross_revenue", "unverifiable", "implication_undecided"),
    ("AGG(net_revenue) * 2 AS gross_revenue", "unverifiable", "implication_undecided"),
])
def test_inside_the_boundary_the_violation_still_stands(tmp_path, projection, kind, code):
    """Controls: a supported slice and grouping do not shield a transformation."""
    sql = f"SELECT channel, {projection} FROM {V} WHERE country = 'US' GROUP BY channel"
    result = run(tmp_path, _rows(sql), view_text=VIEW_WITH)
    (f,) = result["findings"]
    assert engine_kind(f) == kind and engine_codes(f) == [code]
    # Reported under the policy: a violation is a suspected difference.
    assert f["kind"] == ("suspected_difference" if kind == "definition_violation" else kind)
    assert engine_funnel(result["funnel"])["violations"] == (
        1 if kind == "definition_violation" else 0)
    headline_consistent(result)


# ------------------------------------------------------------ raw table

@pytest.mark.parametrize("sql, code", [
    # changed measure + unsupported condition
    ("SELECT SUM(amount) AS net_revenue FROM main.finance.orders "
     "WHERE status = 'completed' AND channel = country", "predicate_unsupported"),
    # dropped required filter + unsupported condition (it could stand in for it)
    (f"SELECT {GOVERNED} AS net_revenue FROM main.finance.orders "
     f"WHERE channel = country", "predicate_unsupported"),
    # transformation + LIMIT (× 2 is a definite change; ROUND is not decided)
    (f"SELECT ({GOVERNED}) * 2 AS net_revenue FROM main.finance.orders "
     f"WHERE status = 'completed' LIMIT 5", "shape_unsupported"),
    # changed measure + HAVING
    ("SELECT channel, SUM(amount) AS net_revenue FROM main.finance.orders "
     "WHERE status = 'completed' GROUP BY channel HAVING SUM(amount) > 0", "shape_unsupported"),
    # changed measure + grouping by an unsupported construct
    ("SELECT channel, SUM(amount) AS net_revenue FROM main.finance.orders "
     "WHERE status = 'completed' GROUP BY ROLLUP (channel)", "shape_unsupported"),
])
def test_raw_table_violation_outside_the_boundary_is_unverifiable(tmp_path, sql, code):
    result = run(tmp_path, _rows(sql), view_text=VIEW_WITH)
    (f,) = result["findings"]
    assert (engine_kind(f), f["concept"]) == ("unverifiable", "net_revenue")
    assert f["kind"] == "unverifiable"
    assert code in engine_codes(f) and codes(f) == engine_codes(f)
    assert f["suspected"]
    assert engine_funnel(result["funnel"])["violations"] == 0
    headline_consistent(result)


def test_raw_table_violation_inside_the_boundary_still_stands(tmp_path):
    sql = ("SELECT channel, SUM(amount) AS net_revenue FROM main.finance.orders "
           "WHERE status = 'completed' AND country = 'US' GROUP BY channel")
    result = run(tmp_path, _rows(sql), view_text=VIEW_WITH)
    (f,) = result["findings"]
    assert engine_kind(f) == "definition_violation" and engine_codes(f) == ["measure_changed"]
    assert f["kind"] == "suspected_difference" and codes(f) == ["measure_changed"]
    assert engine_funnel(result["funnel"])["violations"] == 1
    assert "suspected" not in f
    headline_consistent(result)
