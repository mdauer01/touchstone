"""An adapter may translate a declaration. It may never invent one.

Round-two adversarial review, 2026-07-26. A fabricated claim is worse than a
missing one: it reaches proof grade, gets reconciled against real code, and
produces a CERTIFIED or CONTRADICTION verdict about a definition nobody
wrote. Every test here failed before its fix.
"""

import pytest

from semantix.adapters.agents_schema import claims_from_tables
from semantix.adapters.metrics_yaml import load_metrics


def lookml(measure_sql="${TABLE}.amount", filters=None, mtype="sum"):
    tables = {
        "lookml_view": [{"name": "orders", "sql_table_name": "analytics.fct_orders"}],
        "lookml_measure": [{
            "measure_name": "revenue", "view_name": "orders",
            "sql": measure_sql, "type": mtype,
            **({"filters": filters} if filters is not None else {}),
        }],
    }
    return claims_from_tables(tables)[0]


# ------------------------------------------------------- LookML filter DSL

def test_plain_equality_filter_is_translated():
    spec = lookml(filters='[status: "completed"]')
    assert spec.filters == ["status = 'completed'"]
    assert not spec.untranslatable


def test_negation_preserves_lookers_null_inclusion():
    """Looker's `-value` means "not value", and in Looker that INCLUDES rows
    where the field is NULL. Rendering it as a bare `<>` silently drops those
    rows, making the claim stricter than the declaration it represents — and
    able to falsely match a model that also drops them."""
    spec = lookml(filters='[channel: "-internal"]')
    assert spec.filters == ["(channel <> 'internal' OR channel IS NULL)"]
    assert not spec.untranslatable


@pytest.mark.parametrize("value", [">100", "<=5", "1 to 10", "%partial%",
                                   "NOT NULL", "NULL", "a,b,c", "after 2024-01-01"])
def test_filter_expression_dsl_is_never_fabricated_into_equality(value):
    """LookML filter values are a small language. `>100` is a range, not the
    literal string '>100'. Every one of these was previously turned into
    `field = '<the raw text>'` — a predicate nobody declared."""
    spec = lookml(filters=f'[amount: "{value}"]')
    assert spec.filters == [], f"fabricated a predicate from {value!r}"
    assert spec.untranslatable, f"{value!r} must be reported, not silently dropped"


def test_unparseable_filter_blob_is_not_passed_through_as_sql():
    """A blob the filter regex cannot read used to be emitted verbatim as if
    it were a SQL predicate."""
    spec = lookml(filters="something nobody can parse")
    assert spec.filters == []
    assert spec.untranslatable


def test_untranslatable_filters_make_the_claim_inexpressible(acme, tmp_path):
    """The end-to-end consequence: a claim we cannot fully translate must not
    be reconciled as if we had."""
    from semantix.engine import build_claim
    from semantix.extraction.registry import SchemaRegistry

    spec = lookml(filters='[amount: ">100"]')
    claim = build_claim(spec, SchemaRegistry({"analytics.fct_orders": {"amount": "int"}}), "duckdb")
    assert claim.expressible is False
    assert "translate" in (claim.error or "").lower()


# ------------------------------------------------------- metrics YAML shapes

def test_scalar_filters_are_not_shredded_into_characters(tmp_path):
    """`filters: "status = 'x'"` (a scalar where a list was expected) was
    iterated as a STRING, producing one bogus predicate per character."""
    p = tmp_path / "m.yml"
    p.write_text(
        "metrics:\n"
        "  - name: revenue\n"
        "    model: analytics.fct_orders\n"
        "    expr: sum(amount)\n"
        "    filters: \"status = 'completed'\"\n"
    )
    spec = load_metrics(p)[0]
    assert spec.filters == ["status = 'completed'"]


def test_non_string_filter_values_do_not_become_predicates(tmp_path):
    p = tmp_path / "m.yml"
    p.write_text(
        "metrics:\n"
        "  - name: revenue\n"
        "    model: analytics.fct_orders\n"
        "    expr: sum(amount)\n"
        "    filters:\n"
        "      - {status: completed}\n"
        "    group_by: 42\n"
    )
    spec = load_metrics(p)[0]
    assert spec.filters == []
    assert spec.untranslatable
    assert spec.group_by == []


def test_top_level_list_yaml_does_not_crash(tmp_path):
    p = tmp_path / "m.yml"
    p.write_text("- name: revenue\n  model: a.b\n  expr: sum(x)\n")
    assert load_metrics(p) == [] or load_metrics(p)[0].name == "revenue"
