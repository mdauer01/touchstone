from pathlib import Path

import pytest

from semantix.extraction.registry import SchemaRegistry

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "acme"

CATALOG = {
    "raw.orders": {
        "order_id": "bigint",
        "customer_id": "bigint",
        "order_date": "date",
        "status": "varchar",
        "amount": "decimal(18,2)",
        "refund_amount": "decimal(18,2)",
        "channel": "varchar",
    },
    "raw.customers": {
        "customer_id": "bigint",
        "signup_date": "date",
        "segment": "varchar",
    },
}


@pytest.fixture
def registry() -> SchemaRegistry:
    reg = SchemaRegistry(CATALOG)
    # analytics.fct_orders mirrors raw.orders (the demo estate's mart)
    reg._types["analytics.fct_orders"] = dict(CATALOG["raw.orders"])
    reg._terminals["analytics.fct_orders"] = {
        c: (f"raw.orders.{c}",) for c in CATALOG["raw.orders"]
    }
    return reg


@pytest.fixture
def schema(registry) -> dict:
    return registry.sqlglot_schema("duckdb")


@pytest.fixture
def acme() -> Path:
    return EXAMPLES
